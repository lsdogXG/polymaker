from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


CYCLE_STATES = {
    "CREATED",
    "SUBMITTED",
    "PARTIAL",
    "HEDGED",
    "CONFIRMED",
    "REJECTED",
    "FLATTENED",
    "RESCUE_SUBMITTED",
    "RESCUED",
    "FAILED",
    "EXPIRED",  # FOK orders timed out without any events
}

ORDER_STATUSES = {"PLACEMENT", "UPDATE", "CANCELLATION", "UNKNOWN"}
TRADE_STATUSES = {"MATCHED", "MINED", "CONFIRMED", "RETRYING", "FAILED"}


@dataclass
class CycleSnapshot:
    cycle_id: str
    condition_id: str
    state: str
    created_at: datetime
    submitted_at: datetime | None
    closed_at: datetime | None
    expected_edge: Decimal | None
    realized_edge: Decimal | None
