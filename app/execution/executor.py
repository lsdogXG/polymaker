from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Dict, Iterable

from app.clients.clob import ClobClientWrapper
from app.config import Settings
from app.db.repo import Repo
from app.execution.rescue import attempt_rescue
from app.execution.risk import RiskManager
from app.model.intent import LegIntent, TradeIntent

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _order_notional(legs: Iterable[LegIntent]) -> Decimal:
    total = Decimal("0")
    for leg in legs:
        if leg.side.upper() == "BUY":
            total += leg.price * leg.size
    return total


def _extract_order_id(response: object) -> str | None:
    if isinstance(response, dict):
        for key in ("orderID", "order_id", "id"):
            if key in response:
                return str(response[key])
        data = response.get("data")
        if isinstance(data, dict):
            return _extract_order_id(data)
    return None


def _extract_order_ids(response: object) -> list[str]:
    if isinstance(response, list):
        ids: list[str] = []
        for item in response:
            oid = _extract_order_id(item)
            if oid:
                ids.append(oid)
        return ids
    if isinstance(response, dict):
        if "data" in response:
            return _extract_order_ids(response["data"])
        if "orders" in response:
            return _extract_order_ids(response["orders"])
    oid = _extract_order_id(response)
    return [oid] if oid else []


def _payload_value(payload: dict, *keys: str) -> object | None:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _payload_decimal(payload: dict, *keys: str) -> Decimal | None:
    value = _payload_value(payload, *keys)
    if value is None:
        return None
    return Decimal(str(value))


@dataclass
class CycleContext:
    cycle_id: str
    condition_id: str
    mode: str
    legs: tuple[LegIntent, ...]
    order_ids: Dict[str, str] = field(default_factory=dict)  # token_id -> order_id
    filled: Dict[str, Decimal] = field(default_factory=dict)  # token_id -> size_matched
    notional: Decimal = Decimal("0")
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    unhedged_task: asyncio.Task | None = None
    fast_hedge_task: asyncio.Task | None = None  # 500ms fast hedge attempt
    hedge_order_id: str | None = None
    hedge_timeout_task: asyncio.Task | None = None
    confirmed_tokens: set[str] = field(default_factory=set)
    submitted_timeout_task: asyncio.Task | None = None
    received_any_event: bool = False
    fast_hedge_attempted: bool = False  # Track if we already tried fast hedge

    def is_hedged(self) -> bool:
        if len(self.legs) < 2:
            return False
        return all(self.filled.get(leg.token_id, Decimal("0")) >= leg.size for leg in self.legs)

    def is_unhedged(self) -> bool:
        return any(self.filled.get(leg.token_id, Decimal("0")) > 0 for leg in self.legs) and not self.is_hedged()

    def mark_confirmed(self, token_id: str) -> None:
        if token_id:
            self.confirmed_tokens.add(token_id)

    def all_legs_confirmed(self) -> bool:
        token_ids = {leg.token_id for leg in self.legs}
        return bool(token_ids) and token_ids.issubset(self.confirmed_tokens)


class ExecutionEngine:
    # Timeout for SUBMITTED state - FOK orders should fill or reject quickly
    SUBMITTED_TIMEOUT_SEC = 10.0

    def __init__(
        self,
        settings: Settings,
        repo: Repo,
        clob: ClobClientWrapper,
        risk: RiskManager,
        get_orderbook: Callable[[str], object | None],
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.clob = clob
        self.risk = risk
        self.get_orderbook = get_orderbook
        self.cycles: Dict[str, CycleContext] = {}
        self.order_to_cycle: Dict[str, str] = {}

    async def execute_intent(self, intent: TradeIntent) -> str | None:
        """Create a cycle in Mongo and submit orders. Returns cycle_id if accepted."""
        notional = _order_notional(intent.legs)
        if not self.risk.can_open(intent.condition_id, notional):
            await self.repo.log_audit(
                "risk_block", {"condition_id": intent.condition_id, "notional": str(notional)}
            )
            return None

        params = {
            "entry_buffer": str(self.settings.entry_buffer),
            "fee_buffer": str(self.settings.fee_buffer),
            "target_shares": float(intent.target_shares),
            "limits": {k: float(v) for k, v in intent.limits.items()},
            "order_type": intent.order_type,
            "expected_edge": float(intent.expected_edge),
            "created_at_ms": int(intent.created_at.timestamp() * 1000),
        }
        cycle_id = await self.repo.create_cycle(intent.condition_id, intent.mode, params)
        ctx = CycleContext(
            cycle_id=cycle_id,
            condition_id=intent.condition_id,
            mode=intent.mode,
            legs=intent.legs,
            notional=notional,
        )
        self.cycles[cycle_id] = ctx

        if self.settings.dry_run:
            await self.repo.attach_cycle_legs(
                cycle_id,
                [
                    {
                        "token_id": leg.token_id,
                        "side": leg.side,
                        "outcome": leg.outcome,
                        "order_id": None,
                        "status": "DRY_RUN",
                    }
                    for leg in intent.legs
                ],
            )
            await self.repo.log_audit(
                "dry_run_intent",
                {"cycle_id": cycle_id, "condition_id": intent.condition_id, "params": params},
            )
            await self.repo.close_cycle(cycle_id, "CONFIRMED")
            self.cycles.pop(cycle_id, None)
            return cycle_id

        try:
            if intent.mode == "FULLSET":
                await self._submit_fullset(ctx)
            elif intent.mode == "SINGLE_LEG":
                await self._submit_single_leg(ctx)
            else:
                raise RuntimeError(f"Unknown intent mode {intent.mode}")

            self.risk.register_cycle(intent.condition_id, notional)
            await self.repo.update_cycle_state(
                cycle_id, "SUBMITTED", **{"timestamps.submitted_at": _now_iso()}
            )
            # Start timeout for SUBMITTED state - clean up if no events received
            ctx.submitted_timeout_task = asyncio.create_task(self._submitted_timeout(cycle_id))
            return cycle_id
        except Exception as exc:
            import traceback
            error_msg = f"{type(exc).__name__}: {exc}"
            error_tb = traceback.format_exc()
            logger.error("Order submit failed for cycle %s: %s\n%s", cycle_id, error_msg, error_tb)
            # Record failure for circuit breaker
            self.risk.record_failure(f"order_submit: {error_msg}")
            await self.repo.update_cycle_state(
                cycle_id, "REJECTED", **{"timestamps.closed_at": _now_iso()}
            )
            await self.repo.log_audit("order_submit_failed", {"cycle_id": cycle_id, "error": error_msg, "traceback": error_tb[:500]})
            self.cycles.pop(cycle_id, None)
            return None

    def has_active_cycle(self, condition_id: str) -> bool:
        return any(ctx.condition_id == condition_id for ctx in self.cycles.values())

    async def cleanup_stale_cycles(self, max_age_sec: float = 60.0) -> int:
        """Clean up cycles stuck in SUBMITTED/CREATED state for too long.

        Called on startup to prevent old stuck cycles from blocking new trades.
        Returns number of cycles cleaned up.
        """
        now = datetime.now(timezone.utc)
        cleaned = 0
        for cycle_id in list(self.cycles.keys()):
            ctx = self.cycles.get(cycle_id)
            if not ctx:
                continue
            age = (now - ctx.created_at).total_seconds()
            # Only clean up cycles without any fills
            has_fills = any(v > 0 for v in ctx.filled.values())
            if age > max_age_sec and not has_fills and not ctx.received_any_event:
                logger.warning(
                    "Cleaning up stale cycle %s (age=%.1fs, no fills, no events)",
                    cycle_id, age,
                )
                await self.repo.log_audit(
                    "stale_cycle_cleanup",
                    {"cycle_id": cycle_id, "age_sec": age},
                )
                await self.repo.close_cycle(cycle_id, "EXPIRED")
                self.risk.close_cycle(ctx.condition_id, ctx.notional)
                # Cancel any pending tasks
                for task in [ctx.submitted_timeout_task, ctx.unhedged_task, ctx.fast_hedge_task, ctx.hedge_timeout_task]:
                    if task and not task.done():
                        task.cancel()
                # Clean up order mappings
                for order_id in ctx.order_ids.values():
                    self.order_to_cycle.pop(order_id, None)
                self.cycles.pop(cycle_id, None)
                cleaned += 1
        return cleaned

    def recover_cycle(self, doc: dict) -> None:
        """Re-hydrate in-memory context from Mongo cycle doc."""
        cycle_id = str(doc.get("cycle_id") or doc.get("_id") or "")
        if not cycle_id:
            return

        params = doc.get("params") or {}
        target_shares = Decimal(str(params.get("target_shares", 0)))
        limits = params.get("limits") or {}

        legs: list[LegIntent] = []
        for leg in doc.get("legs") or []:
            token_id = leg.get("token_id")
            outcome = leg.get("outcome") or ""
            if not token_id:
                continue
            price = Decimal(str(limits.get(outcome, leg.get("price", 0))))
            legs.append(
                LegIntent(
                    token_id=str(token_id),
                    outcome=outcome,
                    side=leg.get("side", "BUY"),
                    price=price,
                    size=target_shares,
                    order_type=params.get("order_type", "FOK"),
                )
            )

        if not legs:
            return

        ctx = CycleContext(
            cycle_id=cycle_id,
            condition_id=str(doc.get("condition_id") or ""),
            mode=str(doc.get("mode") or ""),
            legs=tuple(legs),
            notional=_order_notional(legs),
        )

        for leg in doc.get("legs") or []:
            order_id = leg.get("order_id")
            token_id = leg.get("token_id")
            if order_id and token_id:
                ctx.order_ids[str(token_id)] = str(order_id)
                self.order_to_cycle[str(order_id)] = cycle_id

        fills = doc.get("fills") or {}
        if isinstance(fills, dict):
            for token_id, size in fills.items():
                try:
                    ctx.filled[str(token_id)] = Decimal(str(size))
                except Exception:
                    continue

        if ctx.is_unhedged():
            ctx.unhedged_task = asyncio.create_task(self._unhedged_timeout(cycle_id))

        self.cycles[cycle_id] = ctx

    async def _submit_fullset(self, ctx: CycleContext) -> None:
        orders = [
            self.clob.create_limit_order(leg.token_id, leg.side, leg.price, leg.size, leg.tif_seconds)
            for leg in ctx.legs
        ]
        logger.info("Submitting fullset orders for cycle %s: %s", ctx.cycle_id, orders)
        response = self.clob.post_orders(orders, "FOK")
        logger.info("post_orders response for cycle %s: %s (type=%s)", ctx.cycle_id, response, type(response).__name__)
        order_ids = _extract_order_ids(response)
        logger.info("Extracted order_ids: %s", order_ids)
        await self.repo.log_audit(
            "order_submitted", {"cycle_id": ctx.cycle_id, "order_ids": order_ids, "mode": "FULLSET", "response": str(response)[:500]}
        )

        legs_payload: list[dict] = []
        for idx, leg in enumerate(ctx.legs):
            order_id = order_ids[idx] if idx < len(order_ids) else None
            if order_id:
                ctx.order_ids[leg.token_id] = order_id
                self.order_to_cycle[order_id] = ctx.cycle_id
            legs_payload.append(
                {
                    "token_id": leg.token_id,
                    "side": leg.side,
                    "outcome": leg.outcome,
                    "order_id": order_id,
                    "status": "SUBMITTED",
                }
            )
        await self.repo.attach_cycle_legs(ctx.cycle_id, legs_payload)

    async def _submit_single_leg(self, ctx: CycleContext) -> None:
        first_leg, hedge_leg = ctx.legs
        order = self.clob.create_limit_order(first_leg.token_id, first_leg.side, first_leg.price, first_leg.size)
        resp = self.clob.post_order(order, "FOK")
        first_order_id = _extract_order_id(resp)
        await self.repo.log_audit(
            "order_submitted",
            {"cycle_id": ctx.cycle_id, "order_id": first_order_id, "mode": "SINGLE_LEG"},
        )
        if first_order_id:
            ctx.order_ids[first_leg.token_id] = first_order_id
            self.order_to_cycle[first_order_id] = ctx.cycle_id

        hedge_order = self.clob.create_limit_order(
            hedge_leg.token_id,
            hedge_leg.side,
            hedge_leg.price,
            hedge_leg.size,
            hedge_leg.tif_seconds,
        )
        hedge_resp = self.clob.post_order(hedge_order, "GTD")
        hedge_order_id = _extract_order_id(hedge_resp)
        await self.repo.log_audit("hedge_submitted", {"cycle_id": ctx.cycle_id, "order_id": hedge_order_id})
        if hedge_order_id:
            ctx.hedge_order_id = hedge_order_id
            ctx.order_ids[hedge_leg.token_id] = hedge_order_id
            self.order_to_cycle[hedge_order_id] = ctx.cycle_id

        ctx.hedge_timeout_task = asyncio.create_task(self._hedge_timeout(ctx.cycle_id))

        await self.repo.attach_cycle_legs(
            ctx.cycle_id,
            [
                {
                    "token_id": first_leg.token_id,
                    "side": first_leg.side,
                    "outcome": first_leg.outcome,
                    "order_id": first_order_id,
                    "status": "SUBMITTED",
                },
                {
                    "token_id": hedge_leg.token_id,
                    "side": hedge_leg.side,
                    "outcome": hedge_leg.outcome,
                    "order_id": hedge_order_id,
                    "status": "SUBMITTED",
                },
            ],
        )

    async def _submitted_timeout(self, cycle_id: str) -> None:
        """Timeout for SUBMITTED state - if no events received, close as EXPIRED."""
        await asyncio.sleep(self.SUBMITTED_TIMEOUT_SEC)
        ctx = self.cycles.get(cycle_id)
        if not ctx:
            return
        # If we received any events, the cycle is progressing - don't expire it
        if ctx.received_any_event:
            return
        # If any fills received, don't expire
        if any(v > 0 for v in ctx.filled.values()):
            return
        # No events received within timeout - FOK orders likely rejected/cancelled
        logger.warning(
            "Cycle %s timed out in SUBMITTED state with no events - marking as EXPIRED",
            cycle_id,
        )
        await self.repo.log_audit(
            "submitted_timeout",
            {"cycle_id": cycle_id, "timeout_sec": self.SUBMITTED_TIMEOUT_SEC},
        )
        await self.repo.close_cycle(cycle_id, "EXPIRED")
        self.risk.close_cycle(ctx.condition_id, ctx.notional)
        # Clean up order mappings
        for order_id in ctx.order_ids.values():
            self.order_to_cycle.pop(order_id, None)
        self.cycles.pop(cycle_id, None)

    async def _hedge_timeout(self, cycle_id: str) -> None:
        await asyncio.sleep(self.settings.single_leg_timeout_sec)
        ctx = self.cycles.get(cycle_id)
        if not ctx or ctx.is_hedged():
            return
        if ctx.hedge_order_id:
            try:
                self.clob.cancel_order(ctx.hedge_order_id)
                await self.repo.log_audit(
                    "hedge_cancelled", {"cycle_id": ctx.cycle_id, "order_id": ctx.hedge_order_id}
                )
            except Exception as exc:
                logger.warning("cancel hedge failed: %s", exc)
        await self._flatten_cycle(ctx)

    async def _flatten_cycle(self, ctx: CycleContext) -> None:
        filled_leg = next(
            (leg for leg in ctx.legs if ctx.filled.get(leg.token_id, Decimal("0")) > 0), None
        )
        if not filled_leg:
            return

        qty = ctx.filled.get(filled_leg.token_id, Decimal('0'))
        if qty <= 0:
            return

        ob = self.get_orderbook(filled_leg.token_id)
        best_bid = ob.best_bid() if ob and hasattr(ob, "best_bid") else None
        if best_bid is None:
            await self.repo.log_audit("flatten_failed", {"cycle_id": ctx.cycle_id, "reason": "no_best_bid"})
            return

        try:
            from app.model.orderbook import floor_to_tick

            limit = floor_to_tick(best_bid.price, ob.tick_size)
            if limit > best_bid.price:
                await self.repo.log_audit(
                    "flatten_failed", {"cycle_id": ctx.cycle_id, "reason": "not_marketable"}
                )
                return
            order = self.clob.create_limit_order(filled_leg.token_id, "SELL", limit, qty)
            self.clob.post_order(order, "FOK")
            await self.repo.log_audit(
                "flatten_submitted", {"cycle_id": ctx.cycle_id, "token_id": filled_leg.token_id, "price": str(limit)}
            )
        except Exception as exc:
            self.risk.record_failure(f"flatten: {exc}")
            await self.repo.log_audit("flatten_failed", {"cycle_id": ctx.cycle_id, "error": str(exc)})
            return

        await self.repo.close_cycle(ctx.cycle_id, "FLATTENED")
        self.risk.close_cycle(ctx.condition_id, ctx.notional)
        self.cycles.pop(ctx.cycle_id, None)

    async def handle_order_event(self, payload: dict) -> None:
        order_id_val = _payload_value(payload, "order_id", "orderID", "id")
        order_id = str(order_id_val) if order_id_val is not None else ""
        if not order_id:
            return

        cycle_id = self.order_to_cycle.get(order_id)
        size_matched = _payload_decimal(payload, "size_matched", "sizeMatched", "filled_size")
        status = _payload_value(payload, "status") or "UNKNOWN"
        token_id = _payload_value(payload, "asset_id", "token_id", "tokenId")

        # Mark that we received an event for this cycle
        if cycle_id:
            ctx = self.cycles.get(cycle_id)
            if ctx:
                ctx.received_any_event = True
                # Cancel submitted timeout since we got an event
                if ctx.submitted_timeout_task and not ctx.submitted_timeout_task.done():
                    ctx.submitted_timeout_task.cancel()
                    ctx.submitted_timeout_task = None

        await self.repo.upsert_order(
            order_id,
            {
                "order_id": order_id,
                "cycle_id": cycle_id,
                "condition_id": _payload_value(payload, "condition_id", "market_id"),
                "token_id": token_id,
                "outcome": _payload_value(payload, "outcome"),
                "side": _payload_value(payload, "side"),
                "order_type": _payload_value(payload, "order_type", "orderType"),
                "price": _payload_decimal(payload, "price"),
                "size": _payload_decimal(payload, "size"),
                "status": status,
                "size_matched": size_matched or Decimal("0"),
                "original_size": _payload_decimal(payload, "original_size", "originalSize"),
                "raw": {"user_ws_payload": payload},
            },
        )

        if cycle_id and token_id and size_matched is not None:
            ctx = self.cycles.get(cycle_id)
            if not ctx:
                return
            token_id_str = str(token_id)
            ctx.filled[token_id_str] = size_matched
            await self.repo.update_cycle_fills(cycle_id, {k: float(v) for k, v in ctx.filled.items()})

            if ctx.is_hedged():
                if ctx.unhedged_task:
                    ctx.unhedged_task.cancel()
                if ctx.fast_hedge_task:
                    ctx.fast_hedge_task.cancel()
                if ctx.hedge_timeout_task:
                    ctx.hedge_timeout_task.cancel()
                await self.repo.update_cycle_state(cycle_id, "HEDGED")
            elif ctx.is_unhedged() and ctx.unhedged_task is None:
                await self.repo.update_cycle_state(cycle_id, "PARTIAL")
                # Start fast hedge attempt at 500ms
                if ctx.fast_hedge_task is None and not ctx.fast_hedge_attempted:
                    ctx.fast_hedge_task = asyncio.create_task(self._fast_hedge_timeout(cycle_id))
                # Also start slower rescue timeout as backup
                ctx.unhedged_task = asyncio.create_task(self._unhedged_timeout(cycle_id))

    async def handle_trade_event(self, payload: dict) -> None:
        trade_id_val = _payload_value(payload, "trade_id", "tradeID", "id")
        if not trade_id_val:
            return
        trade_id = str(trade_id_val)

        taker_order_id = _payload_value(payload, "taker_order_id", "takerOrderId")
        cycle_id = self.order_to_cycle.get(str(taker_order_id)) if taker_order_id else None

        await self.repo.upsert_trade(
            trade_id,
            {
                "trade_id": trade_id,
                "cycle_id": cycle_id,
                "condition_id": _payload_value(payload, "condition_id", "market_id"),
                "token_id": _payload_value(payload, "asset_id", "token_id"),
                "outcome": _payload_value(payload, "outcome"),
                "side": _payload_value(payload, "side"),
                "price": _payload_decimal(payload, "price"),
                "size": _payload_decimal(payload, "size"),
                "status": _payload_value(payload, "status"),
                "taker_order_id": taker_order_id,
                "maker_orders": _payload_value(payload, "maker_orders"),
                "timestamp": _payload_value(payload, "timestamp"),
                "raw": {"user_ws_payload": payload},
            },
        )

        if not cycle_id:
            return

        status = str(_payload_value(payload, "status") or "")
        token_id_val = _payload_value(payload, "asset_id", "token_id")
        token_id = str(token_id_val) if token_id_val is not None else ""

        if status == "CONFIRMED":
            ctx = self.cycles.get(cycle_id)
            if not ctx:
                return
            ctx.mark_confirmed(token_id)
            if ctx.is_hedged() and ctx.all_legs_confirmed():
                await self.repo.close_cycle(cycle_id, "CONFIRMED")
                self.risk.close_cycle(ctx.condition_id, ctx.notional)
                self.cycles.pop(cycle_id, None)

    async def _fast_hedge_timeout(self, cycle_id: str) -> None:
        """
        Fast hedge attempt at 500ms for single-leg fills.

        If one leg is filled but not the other within 500ms, try aggressive market order
        to complete the pair before the slower rescue flow kicks in.
        """
        await asyncio.sleep(self.settings.fast_hedge_timeout_ms / 1000.0)
        ctx = self.cycles.get(cycle_id)
        if not ctx or ctx.is_hedged() or ctx.fast_hedge_attempted:
            return

        ctx.fast_hedge_attempted = True

        # Find the missing leg
        filled_leg = next(
            (leg for leg in ctx.legs if ctx.filled.get(leg.token_id, Decimal("0")) > 0), None
        )
        missing_leg = next(
            (leg for leg in ctx.legs if ctx.filled.get(leg.token_id, Decimal("0")) == 0), None
        )

        if not filled_leg or not missing_leg:
            return

        qty = ctx.filled.get(filled_leg.token_id, Decimal("0"))
        if qty <= 0:
            return

        # Try aggressive market hedge - use best ask price with small buffer
        ob = self.get_orderbook(missing_leg.token_id)
        if not ob:
            logger.warning("Fast hedge failed for %s: no orderbook", cycle_id)
            return

        best_ask = ob.best_ask() if hasattr(ob, "best_ask") else None
        if not best_ask:
            logger.warning("Fast hedge failed for %s: no best ask", cycle_id)
            return

        try:
            from app.model.orderbook import floor_to_tick

            # Use aggressive price: best_ask + 1 tick to ensure fill
            aggressive_price = floor_to_tick(
                best_ask.price + Decimal("0.01"), ob.tick_size
            )

            order = self.clob.create_limit_order(
                missing_leg.token_id,
                missing_leg.side,
                aggressive_price,
                qty,
            )
            resp = self.clob.post_order(order, "FOK")
            order_id = _extract_order_id(resp)

            await self.repo.log_audit(
                "fast_hedge_submitted",
                {
                    "cycle_id": cycle_id,
                    "order_id": order_id,
                    "token_id": missing_leg.token_id,
                    "price": str(aggressive_price),
                    "size": str(qty),
                },
            )
            logger.info(
                "Fast hedge submitted for cycle %s: %s @ %s",
                cycle_id, missing_leg.token_id, aggressive_price
            )

            if order_id:
                ctx.order_ids[missing_leg.token_id] = order_id
                self.order_to_cycle[order_id] = cycle_id

        except Exception as e:
            logger.warning("Fast hedge failed for %s: %s", cycle_id, e)
            await self.repo.log_audit(
                "fast_hedge_failed", {"cycle_id": cycle_id, "error": str(e)}
            )

    async def _unhedged_timeout(self, cycle_id: str) -> None:
        await asyncio.sleep(self.settings.max_unhedged_sec)
        ctx = self.cycles.get(cycle_id)
        if not ctx or ctx.is_hedged():
            return

        filled_leg = next(
            (leg for leg in ctx.legs if ctx.filled.get(leg.token_id, Decimal("0")) > 0), None
        )
        missing_leg = next(
            (leg for leg in ctx.legs if ctx.filled.get(leg.token_id, Decimal("0")) == 0), None
        )
        if not filled_leg or not missing_leg:
            return

        qty = ctx.filled.get(filled_leg.token_id, Decimal("0"))
        if qty <= 0:
            return

        # Resize legs to the actually filled qty to avoid over-hedging / over-selling.
        if qty != filled_leg.size:
            filled_leg = LegIntent(
                token_id=filled_leg.token_id,
                outcome=filled_leg.outcome,
                side=filled_leg.side,
                price=filled_leg.price,
                size=qty,
                order_type=filled_leg.order_type,
                tif_seconds=filled_leg.tif_seconds,
            )
            missing_leg = LegIntent(
                token_id=missing_leg.token_id,
                outcome=missing_leg.outcome,
                side=missing_leg.side,
                price=missing_leg.price,
                size=qty,
                order_type=missing_leg.order_type,
                tif_seconds=missing_leg.tif_seconds,
            )

        await self.repo.update_cycle_state(cycle_id, "RESCUE_SUBMITTED")
        result_state = await attempt_rescue(
            cycle_id,
            missing_leg=missing_leg,
            filled_leg=filled_leg,
            clob=self.clob,
            repo=self.repo,
            get_orderbook=self.get_orderbook,
        )

        # NOTE: attempt_rescue returns a terminal state immediately after submitting rescue orders.
        # For production-grade behavior, you should keep the cycle alive and close only after WS confirmation.
        if result_state in {"FLATTENED", "RESCUED"}:
            await self.repo.close_cycle(cycle_id, result_state)
            self.risk.close_cycle(ctx.condition_id, ctx.notional)
            self.cycles.pop(cycle_id, None)
        else:
            await self.repo.update_cycle_state(cycle_id, result_state)
