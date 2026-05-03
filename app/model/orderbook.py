from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Iterable


@dataclass
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass
class Orderbook:
    token_id: str
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    tick_size: Decimal = Decimal("0.01")
    last_update_ms: int = 0

    def update_from_book(self, payload: dict) -> None:
        bids_raw = payload.get("bids") or payload.get("buys") or payload.get("buy") or []
        asks_raw = payload.get("asks") or payload.get("sells") or payload.get("sell") or []
        self.bids = _parse_levels(bids_raw, reverse=True)
        self.asks = _parse_levels(asks_raw, reverse=False)
        tick = payload.get("tick_size") or payload.get("tickSize")
        if tick is not None:
            self.tick_size = Decimal(str(tick))
        self._touch()

    def apply_price_change(self, payload: dict) -> bool:
        changes = payload.get("changes") or payload.get("price_changes") or []
        try:
            for change in changes:
                side, price, size = _parse_change(change)
                if side == "BUY":
                    _upsert_level(self.bids, price, size, reverse=True)
                elif side == "SELL":
                    _upsert_level(self.asks, price, size, reverse=False)
        except Exception:
            return False
        self._touch()
        return True

    def apply_tick_size_change(self, payload: dict) -> None:
        tick = payload.get("tick_size") or payload.get("tickSize")
        if tick is None:
            return
        self.tick_size = Decimal(str(tick))
        self._touch()

    def update_from_rest(self, book: object) -> None:
        """Update from py-clob-client OrderBookSummary."""
        # Handle OrderBookSummary from SDK
        if hasattr(book, 'bids') and hasattr(book, 'asks'):
            self.bids = _parse_rest_levels(book.bids, reverse=True)
            self.asks = _parse_rest_levels(book.asks, reverse=False)
        elif isinstance(book, dict):
            self.bids = _parse_levels(book.get('bids', []), reverse=True)
            self.asks = _parse_levels(book.get('asks', []), reverse=False)
        self._touch()

    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    def vwap_cost(self, side: str, size: Decimal) -> Decimal | None:
        levels = self.asks if side == "BUY" else self.bids
        result = vwap_cost_with_limit(levels, size)
        return None if result is None else result[0]

    def _touch(self) -> None:
        self.last_update_ms = int(time.time() * 1000)

    def age_ms(self) -> int:
        """Get orderbook age in milliseconds."""
        if self.last_update_ms == 0:
            return 999999  # Never updated
        return int(time.time() * 1000) - self.last_update_ms

    def is_fresh(self, max_age_ms: int = 200) -> bool:
        """Check if orderbook is fresh (updated within max_age_ms)."""
        return self.age_ms() <= max_age_ms

    def is_stale(self, stale_ms: int = 1500) -> bool:
        """Check if orderbook is stale (older than stale_ms)."""
        return self.age_ms() > stale_ms


def vwap_cost(levels: Iterable[BookLevel], size: Decimal) -> Decimal | None:
    result = vwap_cost_with_limit(levels, size)
    return None if result is None else result[0]


def vwap_cost_with_limit(
    levels: Iterable[BookLevel], size: Decimal
) -> tuple[Decimal, Decimal] | None:
    remaining = size
    total_cost = Decimal("0")
    worst_price = None
    for level in levels:
        if remaining <= 0:
            break
        take = min(remaining, level.size)
        total_cost += level.price * take
        remaining -= take
        worst_price = level.price
    if remaining > 0 or worst_price is None:
        return None
    return total_cost, worst_price


def floor_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return price
    return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def ceil_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return price
    return (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def effective_buy_yes(yes_ask: Decimal | None, no_bid: Decimal | None) -> Decimal | None:
    if yes_ask is None and no_bid is None:
        return None
    if yes_ask is None:
        return Decimal("1") - no_bid
    if no_bid is None:
        return yes_ask
    return min(yes_ask, Decimal("1") - no_bid)


def effective_buy_no(no_ask: Decimal | None, yes_bid: Decimal | None) -> Decimal | None:
    if no_ask is None and yes_bid is None:
        return None
    if no_ask is None:
        return Decimal("1") - yes_bid
    if yes_bid is None:
        return no_ask
    return min(no_ask, Decimal("1") - yes_bid)


def _parse_rest_levels(raw_levels: Iterable, reverse: bool) -> list[BookLevel]:
    """Parse OrderSummary objects from py-clob-client SDK."""
    levels: list[BookLevel] = []
    for item in raw_levels:
        price = getattr(item, 'price', None)
        size = getattr(item, 'size', None)
        if price is None or size is None:
            continue
        level = BookLevel(price=Decimal(str(price)), size=Decimal(str(size)))
        if level.size <= 0:
            continue
        levels.append(level)
    levels.sort(key=lambda lvl: lvl.price, reverse=reverse)
    return levels


def _parse_levels(raw_levels: Iterable, reverse: bool) -> list[BookLevel]:
    levels: list[BookLevel] = []
    for item in raw_levels:
        if isinstance(item, dict):
            price = item.get("price")
            size = item.get("size") or item.get("amount")
        else:
            price, size = item[0], item[1]
        if price is None or size is None:
            continue
        level = BookLevel(price=Decimal(str(price)), size=Decimal(str(size)))
        if level.size <= 0:
            continue
        levels.append(level)
    levels.sort(key=lambda lvl: lvl.price, reverse=reverse)
    return levels


def _parse_change(change: object) -> tuple[str, Decimal, Decimal]:
    if isinstance(change, dict):
        side = change.get("side") or change.get("type")
        price = change.get("price")
        size = change.get("size") or change.get("amount") or change.get("quantity")
    else:
        side, price, size = change[0], change[1], change[2]
    side = side.upper() if isinstance(side, str) else ""
    if side in {"BID", "BUY"}:
        side = "BUY"
    elif side in {"ASK", "SELL"}:
        side = "SELL"
    return side, Decimal(str(price)), Decimal(str(size))


def _upsert_level(levels: list[BookLevel], price: Decimal, size: Decimal, reverse: bool) -> None:
    for idx, level in enumerate(levels):
        if level.price == price:
            if size <= 0:
                levels.pop(idx)
            else:
                levels[idx] = BookLevel(price=price, size=size)
            break
    else:
        if size > 0:
            levels.append(BookLevel(price=price, size=size))
    levels.sort(key=lambda lvl: lvl.price, reverse=reverse)
