from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from app.config import GAMMA_HOST, Settings
from app.model.market import MarketMeta, normalize_outcomes, parse_iso8601

_CLIENT: "GammaClient" | None = None


class GammaClient:
    # Supported assets for 15-min markets
    SUPPORTED_ASSETS = ["btc", "eth", "sol", "xrp"]

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = httpx.AsyncClient(base_url=GAMMA_HOST, timeout=10)
        self.slug_patterns = [re.compile(pat) for pat in settings.btc_slug_patterns]

    async def close(self) -> None:
        await self.http.aclose()

    async def poll_new_markets(self) -> list[MarketMeta]:
        """Poll Gamma API for current 15-min markets for all supported assets.

        Supported assets: BTC, ETH, SOL, XRP
        Only returns active markets that are accepting orders.
        """
        return await self._fetch_current_15m_markets()

    async def _fetch_current_15m_markets(self) -> list[MarketMeta]:
        """Fetch live 15-min markets for all supported assets.

        Market is live when: not closed AND acceptingOrders=True
        Uses asyncio.gather for concurrent fetching to minimize latency.
        """
        import asyncio

        now = time.time()
        current_window_start = int(now // 900) * 900

        async def fetch_asset(asset: str) -> list[MarketMeta]:
            """Fetch market for a single asset."""
            slug = f"{asset}-updown-15m-{current_window_start}"
            try:
                resp = await self.http.get("/markets", params={"slug": slug})
                if resp.status_code == 200:
                    data = resp.json()
                    market_list = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
                    results: list[MarketMeta] = []
                    for m in market_list:
                        if isinstance(m, dict):
                            if m.get("closed") is True:
                                continue
                            if m.get("acceptingOrders") is False:
                                continue
                            parsed = _parse_market(m)
                            if parsed and parsed.status == "ACTIVE":
                                results.append(parsed)
                    return results
            except Exception:
                pass
            return []

        # Concurrent fetch for all 4 assets
        results = await asyncio.gather(
            *[fetch_asset(asset) for asset in self.SUPPORTED_ASSETS],
            return_exceptions=True
        )

        markets: list[MarketMeta] = []
        for result in results:
            if isinstance(result, list):
                markets.extend(result)

        return markets

    async def _fetch_from_events(self) -> list[MarketMeta]:
        """Fetch markets from /events endpoint."""
        params = {
            "limit": 100,
            "order": "createdAt",
            "ascending": "false",
            "closed": "false",
        }
        resp = await self.http.get("/events", params=params)
        resp.raise_for_status()
        data = resp.json()

        events: list[dict[str, Any]]
        if isinstance(data, list):
            events = [e for e in data if isinstance(e, dict)]
        elif isinstance(data, dict):
            raw = data.get("events") or data.get("data") or []
            events = [e for e in raw if isinstance(e, dict)]
        else:
            events = []

        markets: list[MarketMeta] = []
        for event in events:
            # Gamma can return either events with nested markets, or markets directly.
            markets_payload = event.get("markets") if isinstance(event.get("markets"), list) else None
            if markets_payload is None:
                markets_payload = [event]
            for market in markets_payload:
                if not isinstance(market, dict):
                    continue
                parsed = _parse_market(market)
                if parsed and _is_target_market(parsed, self.slug_patterns):
                    markets.append(parsed)
        return markets


async def poll_new_markets() -> list[MarketMeta]:
    if _CLIENT is None:
        raise RuntimeError("GammaClient not initialized")
    return await _CLIENT.poll_new_markets()


def init_gamma(settings: Settings) -> GammaClient:
    global _CLIENT
    _CLIENT = GammaClient(settings)
    return _CLIENT


def _coerce_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _parse_market(payload: dict[str, Any]) -> MarketMeta | None:
    condition_id = payload.get("conditionId") or payload.get("condition_id")
    slug = payload.get("slug") or ""
    question = payload.get("question") or payload.get("title") or ""

    created_at = parse_iso8601(payload.get("createdAt") or payload.get("created_at"))
    end_at = parse_iso8601(payload.get("endDate") or payload.get("end_at") or payload.get("endAt"))

    raw_outcomes = payload.get("outcomes")
    outcomes_list = _coerce_list(raw_outcomes)
    if len(outcomes_list) != 2:
        return None
    outcomes = normalize_outcomes([str(x) for x in outcomes_list])

    token_ids_raw = payload.get("clobTokenIds") or payload.get("clob_token_ids")
    token_ids = _coerce_list(token_ids_raw)
    if len(token_ids) != 2:
        return None

    order_min_size = payload.get("orderMinSize") or payload.get("order_min_size")

    if not condition_id or not slug or not created_at:
        return None

    token_map = {outcomes[0]: str(token_ids[0]), outcomes[1]: str(token_ids[1])}

    status = "ACTIVE"
    if payload.get("isResolved") or payload.get("resolved"):
        status = "RESOLVED"
    elif payload.get("active") is False or payload.get("closed") is True:
        status = "CLOSED"

    try:
        oms = float(order_min_size) if order_min_size is not None else None
    except Exception:
        oms = None

    return MarketMeta(
        condition_id=str(condition_id),
        slug=slug,
        question=question,
        created_at=created_at,
        end_at=end_at,
        outcomes=outcomes,
        token_ids=token_map,
        status=status,
        discovered_via=(),
        order_min_size=oms,
    )


def _is_target_market(market: MarketMeta, slug_patterns: list[re.Pattern]) -> bool:
    if any(pat.fullmatch(market.slug) for pat in slug_patterns):
        return True
    # Fallback: some markets do not follow slug patterns.
    q = market.question.lower()
    if "bitcoin" in q and "15" in q and ("minute" in q or "min" in q) and ("up" in q and "down" in q):
        return True
    return False
