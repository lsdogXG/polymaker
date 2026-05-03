from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict

from app.config import Settings
from app.model.orderbook import Orderbook

logger = logging.getLogger(__name__)


@dataclass
class CircuitBreaker:
    """Circuit breaker for trading risk control."""

    # Daily loss tracking
    daily_loss: Decimal = Decimal("0")
    daily_profit: Decimal = Decimal("0")
    last_reset_date: str = ""

    # Consecutive failure tracking
    consecutive_failures: int = 0
    last_failure_time: float = 0.0

    # Cooldown state
    cooldown_until: float = 0.0
    is_tripped: bool = False
    trip_reason: str = ""

    # Configurable thresholds
    max_daily_loss: Decimal = Decimal("100")  # Max daily loss in USDC
    max_consecutive_failures: int = 5
    failure_cooldown_sec: float = 60.0  # Cooldown after max failures
    failure_window_sec: float = 300.0  # Reset failures after this window

    def check_and_reset_daily(self) -> None:
        """Reset daily counters if new day."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.last_reset_date:
            logger.info(
                "Daily reset: previous P&L=%.2f (profit=%.2f loss=%.2f)",
                float(self.daily_profit - self.daily_loss),
                float(self.daily_profit),
                float(self.daily_loss),
            )
            self.daily_loss = Decimal("0")
            self.daily_profit = Decimal("0")
            self.last_reset_date = today
            self.consecutive_failures = 0
            if self.is_tripped and self.trip_reason == "daily_loss":
                self.is_tripped = False
                self.trip_reason = ""
                logger.info("Circuit breaker reset on new day")

    def record_trade_result(self, pnl: Decimal) -> None:
        """Record a completed trade's P&L."""
        self.check_and_reset_daily()

        if pnl >= 0:
            self.daily_profit += pnl
            self.consecutive_failures = 0
        else:
            self.daily_loss += abs(pnl)
            self._record_failure()

        # Check daily loss limit
        if self.daily_loss >= self.max_daily_loss:
            self._trip("daily_loss", f"Daily loss limit reached: {self.daily_loss}")

    def record_failure(self, reason: str = "") -> None:
        """Record a trade execution failure."""
        self._record_failure()
        logger.warning("Trade failure recorded: %s (consecutive: %d)", reason, self.consecutive_failures)

    def _record_failure(self) -> None:
        """Internal failure recording."""
        now = time.time()

        # Reset if outside failure window
        if now - self.last_failure_time > self.failure_window_sec:
            self.consecutive_failures = 0

        self.consecutive_failures += 1
        self.last_failure_time = now

        # Check consecutive failure limit
        if self.consecutive_failures >= self.max_consecutive_failures:
            self._trip(
                "consecutive_failures",
                f"Too many consecutive failures: {self.consecutive_failures}",
            )
            self.cooldown_until = now + self.failure_cooldown_sec

    def _trip(self, reason: str, message: str) -> None:
        """Trip the circuit breaker."""
        if not self.is_tripped:
            self.is_tripped = True
            self.trip_reason = reason
            logger.error("CIRCUIT BREAKER TRIPPED: %s", message)

    def can_trade(self) -> tuple[bool, str]:
        """Check if trading is allowed."""
        self.check_and_reset_daily()

        # Check cooldown
        now = time.time()
        if self.cooldown_until > now:
            remaining = self.cooldown_until - now
            return False, f"Cooldown active: {remaining:.1f}s remaining"

        # Reset cooldown-based trip after cooldown expires
        if self.is_tripped and self.trip_reason == "consecutive_failures":
            self.is_tripped = False
            self.trip_reason = ""
            self.consecutive_failures = 0
            logger.info("Circuit breaker reset after cooldown")

        if self.is_tripped:
            return False, f"Circuit breaker tripped: {self.trip_reason}"

        return True, ""

    def get_status(self) -> dict:
        """Get circuit breaker status for monitoring."""
        self.check_and_reset_daily()
        return {
            "is_tripped": self.is_tripped,
            "trip_reason": self.trip_reason,
            "daily_pnl": float(self.daily_profit - self.daily_loss),
            "daily_loss": float(self.daily_loss),
            "daily_profit": float(self.daily_profit),
            "max_daily_loss": float(self.max_daily_loss),
            "consecutive_failures": self.consecutive_failures,
            "cooldown_remaining": max(0, self.cooldown_until - time.time()),
        }


@dataclass
class RiskManager:
    """Risk management with circuit breaker integration."""

    settings: Settings
    market_spend: Dict[str, Decimal] = field(default_factory=dict)
    total_spend: Decimal = Decimal("0")
    circuit_breaker: CircuitBreaker = field(default_factory=CircuitBreaker)

    def __post_init__(self) -> None:
        # Configure circuit breaker from settings if available
        if hasattr(self.settings, "max_daily_loss"):
            self.circuit_breaker.max_daily_loss = Decimal(str(self.settings.max_daily_loss))
        if hasattr(self.settings, "max_consecutive_failures"):
            self.circuit_breaker.max_consecutive_failures = self.settings.max_consecutive_failures
        if hasattr(self.settings, "failure_cooldown_sec"):
            self.circuit_breaker.failure_cooldown_sec = self.settings.failure_cooldown_sec

    def is_book_stale(self, ob_yes: Orderbook | None, ob_no: Orderbook | None) -> bool:
        if not ob_yes or not ob_no:
            return True
        now_ms = int(time.time() * 1000)
        return (
            now_ms - ob_yes.last_update_ms > self.settings.stale_book_ms
            or now_ms - ob_no.last_update_ms > self.settings.stale_book_ms
        )

    def can_open(self, condition_id: str, notional: Decimal) -> bool:
        # Check circuit breaker first
        can_trade, reason = self.circuit_breaker.can_trade()
        if not can_trade:
            logger.warning("Trade blocked by circuit breaker: %s", reason)
            return False

        per_market = self.market_spend.get(condition_id, Decimal("0"))
        if notional > self.settings.max_usdc_per_trade:
            return False
        if per_market + notional > self.settings.max_usdc_per_market:
            return False
        if self.total_spend + notional > self.settings.max_total_usdc:
            return False
        return True

    def register_cycle(self, condition_id: str, notional: Decimal) -> None:
        self.market_spend[condition_id] = self.market_spend.get(condition_id, Decimal("0")) + notional
        self.total_spend += notional

    def close_cycle(self, condition_id: str, notional: Decimal, pnl: Decimal | None = None) -> None:
        self.market_spend[condition_id] = max(
            Decimal("0"), self.market_spend.get(condition_id, Decimal("0")) - notional
        )
        self.total_spend = max(Decimal("0"), self.total_spend - notional)

        # Record P&L if provided
        if pnl is not None:
            self.circuit_breaker.record_trade_result(pnl)

    def record_failure(self, reason: str = "") -> None:
        """Record a trade execution failure."""
        self.circuit_breaker.record_failure(reason)

    def get_risk_status(self) -> dict:
        """Get full risk status for monitoring."""
        return {
            "total_spend": float(self.total_spend),
            "max_total": float(self.settings.max_total_usdc),
            "utilization": float(self.total_spend / self.settings.max_total_usdc * 100),
            "market_count": len(self.market_spend),
            "circuit_breaker": self.circuit_breaker.get_status(),
        }
