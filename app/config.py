from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv


CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
WS_BASE = "wss://ws-subscriptions-clob.polymarket.com/ws/"
WS_MARKET = WS_BASE + "market"
WS_USER = WS_BASE + "user"


def _env_decimal(key: str, default: str) -> Decimal:
    val = os.getenv(key, default)
    return Decimal(str(val))


def _env_int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


def _env_float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "y"}


@dataclass(frozen=True)
class Settings:
    private_key: str
    funder_address: str
    signature_type: int
    chain_id: int

    api_key: str | None
    api_secret: str | None
    api_passphrase: str | None

    mongodb_uri: str
    mongodb_db: str

    entry_buffer: Decimal
    fee_buffer: Decimal
    expected_slippage: Decimal  # Explicit slippage buffer for fullset trades
    new_market_window_sec: int
    single_leg_timeout_sec: int
    gift_price: Decimal

    max_usdc_per_trade: Decimal
    max_usdc_per_market: Decimal
    max_total_usdc: Decimal
    max_unhedged_sec: float
    fast_hedge_timeout_ms: int  # Fast timeout for single-leg fills (500ms default)
    max_book_age_ms: int  # Max orderbook age for trading (200ms default)
    stale_book_ms: int  # Stale orderbook threshold for warnings (1500ms default)

    dry_run: bool
    gamma_poll_interval_sec: float
    chunk_max_shares: int
    status_log_interval_sec: float

    btc_slug_patterns: tuple[str, ...]

    # Circuit breaker settings
    max_daily_loss: Decimal
    max_consecutive_failures: int
    failure_cooldown_sec: float

    # Logging settings
    log_dir: str

    # Enhanced strategies (from poly-maker / spike-bot)
    enable_position_merge: bool
    min_merge_size: Decimal
    enable_volatility_filter: bool
    max_volatility_pct: Decimal
    max_spread_pct: Decimal
    enable_stop_loss: bool
    stop_loss_pct: Decimal
    trade_cooldown_sec: float
    max_hold_sec: float


def load_settings() -> Settings:
    load_dotenv(override=True)

    btc_slug_patterns = (
        r"btc-updown-15m-\d+",
        r"btc-up-or-down-15m-\d+",
    )

    return Settings(
        private_key=os.getenv("PRIVATE_KEY", ""),
        funder_address=os.getenv("FUNDER_ADDRESS", ""),
        signature_type=_env_int("SIGNATURE_TYPE", 0),
        chain_id=_env_int("CHAIN_ID", 137),
        api_key=os.getenv("API_KEY") or None,
        api_secret=os.getenv("API_SECRET") or None,
        api_passphrase=os.getenv("API_PASSPHRASE") or None,
        mongodb_uri=os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017"),
        mongodb_db=os.getenv("MONGODB_DB", "polymarket_arb"),
        entry_buffer=_env_decimal("ENTRY_BUFFER", "0.05"),
        fee_buffer=_env_decimal("FEE_BUFFER", "0.002"),
        expected_slippage=_env_decimal("EXPECTED_SLIPPAGE", "0.005"),  # 0.5% slippage
        new_market_window_sec=_env_int("NEW_MARKET_WINDOW_SEC", 60),
        single_leg_timeout_sec=_env_int("SINGLE_LEG_TIMEOUT_SEC", 25),
        gift_price=_env_decimal("GIFT_PRICE", "0.02"),
        max_usdc_per_trade=_env_decimal("MAX_USDC_PER_TRADE", "200"),
        max_usdc_per_market=_env_decimal("MAX_USDC_PER_MARKET", "500"),
        max_total_usdc=_env_decimal("MAX_TOTAL_USDC", "2000"),
        max_unhedged_sec=_env_float("MAX_UNHEDGED_SEC", 1.5),
        fast_hedge_timeout_ms=_env_int("FAST_HEDGE_TIMEOUT_MS", 500),  # 500ms fast response
        max_book_age_ms=_env_int("MAX_BOOK_AGE_MS", 200),  # 200ms freshness requirement
        stale_book_ms=_env_int("STALE_BOOK_MS", 1500),
        dry_run=_env_bool("DRY_RUN", False),
        gamma_poll_interval_sec=_env_float("GAMMA_POLL_INTERVAL_SEC", 1.0),
        chunk_max_shares=_env_int("CHUNK_MAX_SHARES", 200),
        status_log_interval_sec=_env_float("STATUS_LOG_INTERVAL_SEC", 15.0),
        btc_slug_patterns=btc_slug_patterns,
        # Circuit breaker
        max_daily_loss=_env_decimal("MAX_DAILY_LOSS", "100"),
        max_consecutive_failures=_env_int("MAX_CONSECUTIVE_FAILURES", 5),
        failure_cooldown_sec=_env_float("FAILURE_COOLDOWN_SEC", 60.0),
        # Logging
        log_dir=os.getenv("LOG_DIR", "./logs"),
        # Enhanced strategies
        enable_position_merge=_env_bool("ENABLE_POSITION_MERGE", True),
        min_merge_size=_env_decimal("MIN_MERGE_SIZE", "5"),
        enable_volatility_filter=_env_bool("ENABLE_VOLATILITY_FILTER", True),
        max_volatility_pct=_env_decimal("MAX_VOLATILITY_PCT", "0.15"),
        max_spread_pct=_env_decimal("MAX_SPREAD_PCT", "0.05"),
        enable_stop_loss=_env_bool("ENABLE_STOP_LOSS", True),
        stop_loss_pct=_env_decimal("STOP_LOSS_PCT", "-0.05"),
        trade_cooldown_sec=_env_float("TRADE_COOLDOWN_SEC", 30.0),
        max_hold_sec=_env_float("MAX_HOLD_SEC", 900.0),
    )


def validate_required(settings: Settings) -> None:
    missing = [
        name
        for name, value in (
            ("PRIVATE_KEY", settings.private_key),
            ("FUNDER_ADDRESS", settings.funder_address),
            ("SIGNATURE_TYPE", str(settings.signature_type) if settings.signature_type is not None else ""),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required env: {', '.join(missing)}")

    if settings.signature_type not in (0, 1, 2):
        raise RuntimeError("SIGNATURE_TYPE must be 0 (EOA), 1 (POLY_PROXY), or 2 (GNOSIS_SAFE)")
