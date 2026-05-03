"""Enhanced strategies borrowed from poly-maker and spike-bot.

Includes:
- Position Merging (poly-maker): Merge YES+NO positions to free capital
- Stop-Loss (poly-maker): Exit positions at loss threshold
- Volatility Filter (poly-maker): Skip trading during high volatility
- Holding Time Limit (spike-bot): Auto-exit after max hold time
- Cooldown Period (spike-bot): Prevent rapid-fire trading
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

logger = logging.getLogger(__name__)

# Constants
MIN_MERGE_SIZE = Decimal("5")  # Minimum shares to trigger merge
USDC_DECIMALS = 6  # USDC has 6 decimals


@dataclass
class PositionMerger:
    """Merges opposing YES/NO positions to free up locked capital.

    From poly-maker: When you hold both YES and NO tokens for the same market,
    they can be redeemed together for $1 each, freeing up your capital.
    """

    min_merge_size: Decimal = MIN_MERGE_SIZE

    async def check_and_merge(
        self,
        clob,
        condition_id: str,
        token_yes: str,
        token_no: str,
        neg_risk: bool = False,
    ) -> Decimal:
        """Check if positions can be merged and execute merge.

        Returns: Amount of USDC freed (0 if no merge)
        """
        try:
            # Get raw position sizes
            pos_yes_raw, _ = clob.get_position(token_yes)
            pos_no_raw, _ = clob.get_position(token_no)

            # Convert to decimal shares
            pos_yes = Decimal(str(pos_yes_raw)) / Decimal(10**USDC_DECIMALS)
            pos_no = Decimal(str(pos_no_raw)) / Decimal(10**USDC_DECIMALS)

            # Calculate mergeable amount
            merge_amount = min(pos_yes, pos_no)

            if merge_amount < self.min_merge_size:
                return Decimal("0")

            logger.info(
                "Position merge opportunity: YES=%.2f NO=%.2f mergeable=%.2f",
                float(pos_yes),
                float(pos_no),
                float(merge_amount),
            )

            # Execute merge (convert back to raw units)
            raw_amount = int(merge_amount * Decimal(10**USDC_DECIMALS))
            clob.merge_positions(raw_amount, condition_id, neg_risk)

            logger.info(
                "Merged %.2f shares for condition=%s, freed ~$%.2f",
                float(merge_amount),
                condition_id,
                float(merge_amount),  # 1 YES + 1 NO = $1
            )
            return merge_amount

        except Exception as e:
            logger.warning("Position merge failed: %s", e)
            return Decimal("0")


@dataclass
class VolatilityFilter:
    """Filters out high-volatility conditions.

    From poly-maker: Don't enter new positions when volatility is too high.
    """

    # Price change thresholds
    max_3h_volatility: Decimal = Decimal("0.15")  # 15% max 3-hour volatility
    max_spread_pct: Decimal = Decimal("0.05")  # 5% max bid-ask spread

    # Price tracking for volatility calculation
    price_history: dict[str, list[tuple[float, Decimal]]] = field(default_factory=dict)
    history_window_sec: float = 10800  # 3 hours

    def record_price(self, token_id: str, price: Decimal) -> None:
        """Record price for volatility calculation."""
        now = time.time()
        if token_id not in self.price_history:
            self.price_history[token_id] = []

        self.price_history[token_id].append((now, price))

        # Prune old entries
        cutoff = now - self.history_window_sec
        self.price_history[token_id] = [
            (t, p) for t, p in self.price_history[token_id] if t > cutoff
        ]

    def get_volatility(self, token_id: str) -> Decimal | None:
        """Calculate volatility over the history window."""
        if token_id not in self.price_history:
            return None
        history = self.price_history[token_id]
        if len(history) < 2:
            return None

        prices = [p for _, p in history]
        min_price = min(prices)
        max_price = max(prices)

        if min_price <= 0:
            return None

        return (max_price - min_price) / min_price

    def should_skip(self, token_id: str, best_bid: Decimal | None, best_ask: Decimal | None) -> tuple[bool, str]:
        """Check if trading should be skipped due to volatility.

        Returns: (should_skip, reason)
        """
        # Check spread
        if best_bid and best_ask and best_bid > 0:
            spread_pct = (best_ask - best_bid) / best_bid
            if spread_pct > self.max_spread_pct:
                return True, f"Spread too wide: {float(spread_pct):.1%}"

        # Check volatility
        volatility = self.get_volatility(token_id)
        if volatility is not None and volatility > self.max_3h_volatility:
            return True, f"Volatility too high: {float(volatility):.1%}"

        return False, ""


@dataclass
class StopLossManager:
    """Stop-loss management for open positions.

    From poly-maker: Exit positions when loss exceeds threshold.
    """

    stop_loss_pct: Decimal = Decimal("-0.05")  # -5% loss triggers stop
    spread_threshold: Decimal = Decimal("0.03")  # Max spread to execute stop-loss

    def check_stop_loss(
        self,
        avg_price: Decimal,
        current_price: Decimal,
        spread: Decimal,
    ) -> tuple[bool, str]:
        """Check if stop-loss should trigger.

        Returns: (should_stop, reason)
        """
        if avg_price <= 0:
            return False, ""

        pnl_pct = (current_price - avg_price) / avg_price

        if pnl_pct < self.stop_loss_pct and spread <= self.spread_threshold:
            return True, f"Stop-loss triggered: PnL={float(pnl_pct):.1%}"

        return False, ""


@dataclass
class TradingControls:
    """Trading rate controls.

    From spike-bot: Prevent rapid trading, enforce holding limits.
    """

    # Cooldown settings
    cooldown_sec: float = 30.0  # Seconds between trades on same market
    last_trade_times: dict[str, float] = field(default_factory=dict)

    # Holding time limit
    max_hold_sec: float = 900.0  # 15 minutes max hold
    entry_times: dict[str, float] = field(default_factory=dict)

    def record_trade(self, condition_id: str) -> None:
        """Record a trade for cooldown tracking."""
        self.last_trade_times[condition_id] = time.time()

    def record_entry(self, condition_id: str) -> None:
        """Record entry time for holding limit."""
        self.entry_times[condition_id] = time.time()

    def clear_entry(self, condition_id: str) -> None:
        """Clear entry time when position closed."""
        self.entry_times.pop(condition_id, None)

    def is_in_cooldown(self, condition_id: str) -> tuple[bool, float]:
        """Check if market is in cooldown.

        Returns: (in_cooldown, remaining_seconds)
        """
        last_trade = self.last_trade_times.get(condition_id, 0)
        elapsed = time.time() - last_trade
        remaining = self.cooldown_sec - elapsed

        if remaining > 0:
            return True, remaining
        return False, 0

    def check_hold_limit(self, condition_id: str) -> tuple[bool, float]:
        """Check if position has exceeded hold limit.

        Returns: (exceeded, hold_time_seconds)
        """
        entry_time = self.entry_times.get(condition_id)
        if entry_time is None:
            return False, 0

        hold_time = time.time() - entry_time
        if hold_time > self.max_hold_sec:
            return True, hold_time

        return False, hold_time


@dataclass
class EnhancedRiskControls:
    """Combined enhanced risk controls from multiple projects."""

    position_merger: PositionMerger = field(default_factory=PositionMerger)
    volatility_filter: VolatilityFilter = field(default_factory=VolatilityFilter)
    stop_loss_manager: StopLossManager = field(default_factory=StopLossManager)
    trading_controls: TradingControls = field(default_factory=TradingControls)

    def configure(
        self,
        min_merge_size: Decimal | None = None,
        max_3h_volatility: Decimal | None = None,
        max_spread_pct: Decimal | None = None,
        stop_loss_pct: Decimal | None = None,
        cooldown_sec: float | None = None,
        max_hold_sec: float | None = None,
    ) -> None:
        """Configure all controls with custom values."""
        if min_merge_size is not None:
            self.position_merger.min_merge_size = min_merge_size
        if max_3h_volatility is not None:
            self.volatility_filter.max_3h_volatility = max_3h_volatility
        if max_spread_pct is not None:
            self.volatility_filter.max_spread_pct = max_spread_pct
        if stop_loss_pct is not None:
            self.stop_loss_manager.stop_loss_pct = stop_loss_pct
        if cooldown_sec is not None:
            self.trading_controls.cooldown_sec = cooldown_sec
        if max_hold_sec is not None:
            self.trading_controls.max_hold_sec = max_hold_sec

    def pre_trade_check(
        self,
        condition_id: str,
        token_id: str,
        best_bid: Decimal | None,
        best_ask: Decimal | None,
    ) -> tuple[bool, str]:
        """Run all pre-trade checks.

        Returns: (can_trade, reason_if_blocked)
        """
        # Check cooldown
        in_cooldown, remaining = self.trading_controls.is_in_cooldown(condition_id)
        if in_cooldown:
            return False, f"Cooldown: {remaining:.1f}s remaining"

        # Check volatility
        skip, reason = self.volatility_filter.should_skip(token_id, best_bid, best_ask)
        if skip:
            return False, reason

        return True, ""

    def get_status(self) -> dict:
        """Get status of all controls for monitoring."""
        return {
            "positions_tracked": len(self.position_merger.price_history if hasattr(self.position_merger, 'price_history') else {}),
            "volatility_tokens": len(self.volatility_filter.price_history),
            "active_cooldowns": sum(
                1 for cid in self.trading_controls.last_trade_times
                if self.trading_controls.is_in_cooldown(cid)[0]
            ),
            "active_holds": len(self.trading_controls.entry_times),
        }
