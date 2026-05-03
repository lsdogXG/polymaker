from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class RuntimeStats:
    gamma_polls: int = 0
    gamma_new: int = 0
    book_updates: int = 0
    order_events: int = 0
    trade_events: int = 0
    intents_fullset: int = 0
    intents_single_leg: int = 0
    last_gamma_poll_at: float | None = None
    last_market_event_at: float | None = None
    last_user_event_at: float | None = None
    last_intent: dict[str, Any] | None = None

    def mark_gamma_poll(self, new_count: int) -> None:
        self.gamma_polls += 1
        self.gamma_new += new_count
        self.last_gamma_poll_at = time.time()

    def mark_book_update(self) -> None:
        self.book_updates += 1
        self.last_market_event_at = time.time()

    def mark_order_event(self) -> None:
        self.order_events += 1
        self.last_user_event_at = time.time()

    def mark_trade_event(self) -> None:
        self.trade_events += 1
        self.last_user_event_at = time.time()

    def mark_intent(self, info: dict[str, Any], mode: str) -> None:
        if mode == "FULLSET":
            self.intents_fullset += 1
        elif mode == "SINGLE_LEG":
            self.intents_single_leg += 1
        self.last_intent = info

    def snapshot(self) -> dict[str, Any]:
        return {
            "gamma_polls": self.gamma_polls,
            "gamma_new": self.gamma_new,
            "book_updates": self.book_updates,
            "order_events": self.order_events,
            "trade_events": self.trade_events,
            "intents_fullset": self.intents_fullset,
            "intents_single_leg": self.intents_single_leg,
            "last_gamma_poll_at": self.last_gamma_poll_at,
            "last_market_event_at": self.last_market_event_at,
            "last_user_event_at": self.last_user_event_at,
            "last_intent": self.last_intent,
        }
