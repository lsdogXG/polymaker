from __future__ import annotations

import logging
from decimal import Decimal
from typing import Callable

from app.clients.clob import ClobClientWrapper
from app.db.repo import Repo
from app.model.intent import LegIntent
from app.model.orderbook import ceil_to_tick, floor_to_tick

logger = logging.getLogger(__name__)


def _marketable_buy(limit: Decimal, best_ask: Decimal) -> bool:
    return limit >= best_ask


def _marketable_sell(limit: Decimal, best_bid: Decimal) -> bool:
    return limit <= best_bid


async def attempt_rescue(
    cycle_id: str,
    missing_leg: LegIntent,
    filled_leg: LegIntent,
    clob: ClobClientWrapper,
    repo: Repo,
    get_orderbook: Callable[[str], object | None],
) -> str:
    ob_missing = get_orderbook(missing_leg.token_id)
    if ob_missing and hasattr(ob_missing, "best_ask"):
        best_ask = ob_missing.best_ask()
        if best_ask:
            limit = floor_to_tick(best_ask.price, ob_missing.tick_size)
            if _marketable_buy(limit, best_ask.price):
                try:
                    order = clob.create_limit_order(
                        missing_leg.token_id, "BUY", limit, missing_leg.size
                    )
                    clob.post_order(order, "FOK")
                    await repo.log_audit(
                        "rescue_fill_missing",
                        {"cycle_id": cycle_id, "token_id": missing_leg.token_id, "price": str(limit)},
                    )
                    return "RESCUED"
                except Exception as exc:
                    logger.warning("rescue fill missing failed: %s", exc)

    ob_filled = get_orderbook(filled_leg.token_id)
    if ob_filled and hasattr(ob_filled, "best_bid"):
        best_bid = ob_filled.best_bid()
        if best_bid:
            limit = floor_to_tick(best_bid.price, ob_filled.tick_size)
            if _marketable_sell(limit, best_bid.price):
                try:
                    order = clob.create_limit_order(
                        filled_leg.token_id, "SELL", limit, filled_leg.size
                    )
                    clob.post_order(order, "FOK")
                    await repo.log_audit(
                        "rescue_flatten",
                        {"cycle_id": cycle_id, "token_id": filled_leg.token_id, "price": str(limit)},
                    )
                    return "FLATTENED"
                except Exception as exc:
                    logger.warning("rescue flatten failed: %s", exc)

    await repo.log_audit(
        "rescue_failed",
        {"cycle_id": cycle_id, "reason": "no_marketable_price"},
    )
    return "FAILED"
