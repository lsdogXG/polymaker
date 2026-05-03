from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(
    level: int = logging.INFO,
    log_dir: str | None = None,
    max_bytes: int = 10 * 1024 * 1024,  # 10MB
    backup_count: int = 5,
) -> None:
    """Setup structured logging with console and file output.

    Args:
        level: Logging level
        log_dir: Directory for log files (default: ./logs)
        max_bytes: Max size per log file before rotation
        backup_count: Number of backup files to keep
    """
    # Create log directory
    if log_dir is None:
        log_dir = os.getenv("LOG_DIR", "./logs")
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Root logger configuration
    root = logging.getLogger()
    root.setLevel(level)

    # Clear existing handlers
    root.handlers.clear()

    # Console handler - concise format
    console_fmt = "%(asctime)s %(levelname)-8s %(name)-20s %(message)s"
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter(console_fmt, datefmt="%H:%M:%S"))
    root.addHandler(console_handler)

    # File handler - detailed format with rotation
    today = datetime.now().strftime("%Y-%m-%d")
    file_fmt = "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s"
    file_handler = RotatingFileHandler(
        log_path / f"arb_{today}.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(file_fmt))
    root.addHandler(file_handler)

    # Trade-specific log file
    trade_handler = RotatingFileHandler(
        log_path / f"trades_{today}.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    trade_handler.setLevel(logging.INFO)
    trade_handler.setFormatter(logging.Formatter(file_fmt))
    trade_handler.addFilter(TradeFilter())
    root.addHandler(trade_handler)

    # Error-only log file
    error_handler = RotatingFileHandler(
        log_path / f"errors_{today}.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter(file_fmt))
    root.addHandler(error_handler)

    # Suppress noisy libraries
    for lib in ["httpx", "httpcore", "websockets", "urllib3", "asyncio"]:
        logging.getLogger(lib).setLevel(logging.WARNING)

    logging.info("Logging initialized: console=%s file=%s", level, log_path)


class TradeFilter(logging.Filter):
    """Filter to capture trade-related log messages."""

    TRADE_KEYWORDS = (
        "intent",
        "cycle",
        "order",
        "trade",
        "fill",
        "hedge",
        "rescue",
        "confirmed",
        "submitted",
        "circuit",
        "risk",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage().lower()
        return any(kw in msg for kw in self.TRADE_KEYWORDS)


def get_trade_logger() -> logging.Logger:
    """Get a logger specifically for trade operations."""
    return logging.getLogger("app.trades")
