from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class LegIntent:
    token_id: str
    outcome: str
    side: str
    price: Decimal
    size: Decimal
    order_type: str
    tif_seconds: int | None = None


@dataclass(frozen=True)
class TradeIntent:
    mode: str
    condition_id: str
    slug: str
    target_shares: Decimal
    limits: dict[str, Decimal]
    expected_edge: Decimal
    order_type: str
    legs: tuple[LegIntent, ...]
    created_at: datetime
