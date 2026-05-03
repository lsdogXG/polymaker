from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.model.orderbook import Orderbook, vwap_cost_with_limit


@dataclass(frozen=True)
class SizeResult:
    size: Decimal
    limit_yes: Decimal
    limit_no: Decimal
    fullset_cost: Decimal
    slippage_adjusted_cost: Decimal  # (yes + no) * (1 + slippage)


def is_profitable_with_slippage(
    ask_yes: Decimal,
    ask_no: Decimal,
    expected_slippage: Decimal,
    fee_buffer: Decimal,
) -> tuple[bool, Decimal]:
    """
    Check if trade is profitable considering slippage.

    Formula: (Ask_Yes + Ask_No) * (1 + Expected_Slippage) < 1 - fee_buffer

    Returns (is_profitable, slippage_adjusted_cost)
    """
    combined = ask_yes + ask_no
    slippage_adjusted = combined * (Decimal("1") + expected_slippage)
    threshold = Decimal("1") - fee_buffer
    return slippage_adjusted < threshold, slippage_adjusted


def compute_optimal_size(
    ob_yes: Orderbook,
    ob_no: Orderbook,
    min_shares: Decimal,
    entry_buffer: Decimal,
    fee_buffer: Decimal,
    max_usdc_per_trade: Decimal,
    max_usdc_per_market: Decimal,
    max_total_usdc: Decimal,
    current_market_spend: Decimal = Decimal("0"),
    current_total_spend: Decimal = Decimal("0"),
    expected_slippage: Decimal = Decimal("0.005"),  # 0.5% default
) -> SizeResult | None:
    """
    Compute optimal trade size using exponential search.

    Trade condition: (VWAP_Yes + VWAP_No) * (1 + slippage) < 1 - fee_buffer

    This ensures profitable fullset even after accounting for execution slippage.
    """
    size = min_shares
    best: SizeResult | None = None

    while True:
        yes_result = vwap_cost_with_limit(ob_yes.asks, size)
        no_result = vwap_cost_with_limit(ob_no.asks, size)
        if yes_result is None or no_result is None:
            break

        cost_yes, limit_yes = yes_result
        cost_no, limit_no = no_result
        fullset_cost = (cost_yes + cost_no) / size

        # New slippage-aware profitability check
        # Formula: fullset_cost * (1 + slippage) < 1 - fee_buffer
        slippage_adjusted = fullset_cost * (Decimal("1") + expected_slippage)
        threshold = Decimal("1") - fee_buffer
        edge_ok = slippage_adjusted < threshold

        # Also apply legacy entry_buffer check for backward compatibility
        legacy_ok = fullset_cost <= Decimal("1") - entry_buffer - fee_buffer

        notional = size * fullset_cost
        budget_ok = (
            notional <= max_usdc_per_trade
            and current_market_spend + notional <= max_usdc_per_market
            and current_total_spend + notional <= max_total_usdc
        )

        if edge_ok and legacy_ok and budget_ok:
            best = SizeResult(
                size=size,
                limit_yes=limit_yes,
                limit_no=limit_no,
                fullset_cost=fullset_cost,
                slippage_adjusted_cost=slippage_adjusted,
            )
            size = size * 2
            continue
        break

    return best
