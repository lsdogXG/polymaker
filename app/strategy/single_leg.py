from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.model.intent import LegIntent, TradeIntent
from app.model.market import MarketMeta
from app.model.orderbook import (
    Orderbook,
    effective_buy_no,
    effective_buy_yes,
    floor_to_tick,
)
from app.strategy.context import get_settings


def evaluate_single_leg(
    market: MarketMeta, ob_yes: Orderbook, ob_no: Orderbook, now: datetime
) -> TradeIntent | None:
    settings = get_settings()
    if (now - market.created_at).total_seconds() > settings.new_market_window_sec:
        return None

    yes_best_ask = ob_yes.best_ask()
    yes_best_bid = ob_yes.best_bid()
    no_best_ask = ob_no.best_ask()
    no_best_bid = ob_no.best_bid()

    effective_yes = effective_buy_yes(
        yes_best_ask.price if yes_best_ask else None,
        no_best_bid.price if no_best_bid else None,
    )
    effective_no = effective_buy_no(
        no_best_ask.price if no_best_ask else None,
        yes_best_bid.price if yes_best_bid else None,
    )

    if effective_yes is None and effective_no is None:
        return None

    outcome_a, outcome_b = market.outcomes
    token_a = market.token_ids[outcome_a]
    token_b = market.token_ids[outcome_b]

    pick_outcome = None
    if effective_yes is not None and effective_yes <= settings.gift_price:
        pick_outcome = outcome_a
    if effective_no is not None and effective_no <= settings.gift_price:
        if pick_outcome is None or effective_no < effective_yes:
            pick_outcome = outcome_b

    if pick_outcome is None:
        return None

    first_ob = ob_yes if pick_outcome == outcome_a else ob_no
    first_best_ask = first_ob.best_ask()
    if first_best_ask is None:
        return None

    min_shares = Decimal(str(market.order_min_size or 10))
    limit_first = floor_to_tick(first_best_ask.price, first_ob.tick_size)
    if limit_first < first_best_ask.price:
        return None

    max_fullset_cost = Decimal("1") - settings.entry_buffer - settings.fee_buffer
    hedge_limit = max_fullset_cost - limit_first
    if hedge_limit <= 0:
        return None

    hedge_ob = ob_no if pick_outcome == outcome_a else ob_yes
    hedge_limit = floor_to_tick(hedge_limit, hedge_ob.tick_size)

    legs = (
        LegIntent(
            token_id=token_a if pick_outcome == outcome_a else token_b,
            outcome=pick_outcome,
            side="BUY",
            price=limit_first,
            size=min_shares,
            order_type="FOK",
        ),
        LegIntent(
            token_id=token_b if pick_outcome == outcome_a else token_a,
            outcome=outcome_b if pick_outcome == outcome_a else outcome_a,
            side="BUY",
            price=hedge_limit,
            size=min_shares,
            order_type="GTD",
            tif_seconds=settings.single_leg_timeout_sec,
        ),
    )

    expected_edge = Decimal("1") - (limit_first + hedge_limit) - settings.fee_buffer

    return TradeIntent(
        mode="SINGLE_LEG",
        condition_id=market.condition_id,
        slug=market.slug,
        target_shares=min_shares,
        limits={pick_outcome: limit_first, (outcome_b if pick_outcome == outcome_a else outcome_a): hedge_limit},
        expected_edge=expected_edge,
        order_type="MIXED",
        legs=legs,
        created_at=now,
    )
