from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.model.intent import LegIntent, TradeIntent
from app.model.market import MarketMeta
from app.model.orderbook import Orderbook, effective_buy_no, effective_buy_yes, floor_to_tick
from app.strategy.context import get_settings
from app.strategy.sizing import compute_optimal_size


def evaluate_fullset(
    market: MarketMeta, ob_yes: Orderbook, ob_no: Orderbook
) -> TradeIntent | None:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    min_shares = Decimal(str(market.order_min_size or 10))

    size_result = compute_optimal_size(
        ob_yes=ob_yes,
        ob_no=ob_no,
        min_shares=min_shares,
        entry_buffer=settings.entry_buffer,
        fee_buffer=settings.fee_buffer,
        max_usdc_per_trade=settings.max_usdc_per_trade,
        max_usdc_per_market=settings.max_usdc_per_market,
        max_total_usdc=settings.max_total_usdc,
        expected_slippage=settings.expected_slippage,
    )
    if size_result is None:
        return None

    best_yes = ob_yes.best_ask()
    best_no = ob_no.best_ask()
    if best_yes is None or best_no is None:
        return None
    yes_bid = ob_yes.best_bid()
    no_bid = ob_no.best_bid()

    limit_yes = floor_to_tick(size_result.limit_yes, ob_yes.tick_size)
    limit_no = floor_to_tick(size_result.limit_no, ob_no.tick_size)

    if limit_yes < best_yes.price or limit_no < best_no.price:
        return None

    effective_yes = effective_buy_yes(best_yes.price, no_bid.price if no_bid else None)
    effective_no = effective_buy_no(best_no.price, yes_bid.price if yes_bid else None)
    if effective_yes is not None and effective_no is not None:
        expected_edge = Decimal("1") - (effective_yes + effective_no) - settings.fee_buffer
    else:
        expected_edge = Decimal("1") - size_result.fullset_cost - settings.fee_buffer

    outcome_a, outcome_b = market.outcomes
    token_yes = market.token_ids[outcome_a]
    token_no = market.token_ids[outcome_b]

    legs = (
        LegIntent(
            token_id=token_yes,
            outcome=outcome_a,
            side="BUY",
            price=limit_yes,
            size=size_result.size,
            order_type="FOK",
        ),
        LegIntent(
            token_id=token_no,
            outcome=outcome_b,
            side="BUY",
            price=limit_no,
            size=size_result.size,
            order_type="FOK",
        ),
    )

    return TradeIntent(
        mode="FULLSET",
        condition_id=market.condition_id,
        slug=market.slug,
        target_shares=size_result.size,
        limits={outcome_a: limit_yes, outcome_b: limit_no},
        expected_edge=expected_edge,
        order_type="FOK",
        legs=legs,
        created_at=now,
    )
