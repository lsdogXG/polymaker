from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable


@dataclass(frozen=True)
class MarketMeta:
    condition_id: str
    slug: str
    question: str
    created_at: datetime
    end_at: datetime | None
    outcomes: tuple[str, str]
    token_ids: dict[str, str]
    status: str
    discovered_via: tuple[str, ...]
    order_min_size: float | None = None

    @property
    def condition(self) -> str:
        return self.condition_id

    def is_active(self) -> bool:
        return self.status.upper() == "ACTIVE"


def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


def normalize_outcomes(raw: Iterable[str]) -> tuple[str, str]:
    outcomes = tuple(raw)
    if len(outcomes) != 2:
        return ("OUTCOME_A", "OUTCOME_B")
    return outcomes[0], outcomes[1]
