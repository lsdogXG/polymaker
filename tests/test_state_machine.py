import asyncio
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from app.config import Settings
from app.execution.executor import CycleContext, ExecutionEngine
from app.execution.risk import RiskManager
from app.model.intent import LegIntent


@dataclass
class FakeRepo:
    states: list[str] = field(default_factory=list)
    closed_states: list[str] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)
    orders: dict = field(default_factory=dict)

    async def update_cycle_state(self, cycle_id: str, state: str, **kwargs):
        self.states.append(state)

    async def update_cycle_fills(self, cycle_id: str, fills: dict):
        self.fills.append(fills)

    async def close_cycle(self, cycle_id: str, state: str, realized_edge=None):
        self.closed_states.append(state)

    async def upsert_order(self, order_id: str, payload: dict):
        self.orders[order_id] = payload

    async def upsert_trade(self, trade_id: str, payload: dict):
        return None

    async def log_audit(self, event: str, payload: dict):
        return None

    async def attach_cycle_legs(self, cycle_id: str, legs: list[dict]):
        return None


class DummyClob:
    pass


def _settings() -> Settings:
    return Settings(
        private_key="x",
        funder_address="y",
        signature_type=0,
        chain_id=137,
        api_key="k",
        api_secret="s",
        api_passphrase="p",
        mongodb_uri="mongodb://localhost:27017",
        mongodb_db="polymarket_arb",
        entry_buffer=Decimal("0.05"),
        fee_buffer=Decimal("0.002"),
        new_market_window_sec=60,
        single_leg_timeout_sec=1,
        gift_price=Decimal("0.02"),
        max_usdc_per_trade=Decimal("1000"),
        max_usdc_per_market=Decimal("2000"),
        max_total_usdc=Decimal("5000"),
        max_unhedged_sec=0.01,
        stale_book_ms=1500,
        dry_run=True,
        gamma_poll_interval_sec=1.0,
        chunk_max_shares=200,
        btc_slug_patterns=("btc-updown-15m-\\d+",),
    )


@pytest.mark.asyncio
async def test_cycle_partial_to_hedged() -> None:
    settings = _settings()
    repo = FakeRepo()
    risk = RiskManager(settings)
    engine = ExecutionEngine(settings, repo, DummyClob(), risk, lambda tid: None)

    leg_a = LegIntent(
        token_id="A",
        outcome="UP",
        side="BUY",
        price=Decimal("0.4"),
        size=Decimal("10"),
        order_type="FOK",
    )
    leg_b = LegIntent(
        token_id="B",
        outcome="DOWN",
        side="BUY",
        price=Decimal("0.5"),
        size=Decimal("10"),
        order_type="FOK",
    )
    ctx = CycleContext(cycle_id="c1", condition_id="cond", mode="FULLSET", legs=(leg_a, leg_b))
    engine.cycles["c1"] = ctx
    engine.order_to_cycle["o1"] = "c1"
    engine.order_to_cycle["o2"] = "c1"

    await engine.handle_order_event({"order_id": "o1", "asset_id": "A", "size_matched": "5"})
    assert "PARTIAL" in repo.states

    await engine.handle_order_event({"order_id": "o1", "asset_id": "A", "size_matched": "10"})
    await engine.handle_order_event({"order_id": "o2", "asset_id": "B", "size_matched": "10"})
    assert "HEDGED" in repo.states


@pytest.mark.asyncio
async def test_unhedged_timeout_triggers_rescue(monkeypatch) -> None:
    async def fake_rescue(*args, **kwargs):
        return "FLATTENED"

    monkeypatch.setattr("app.execution.executor.attempt_rescue", fake_rescue)

    settings = _settings()
    repo = FakeRepo()
    risk = RiskManager(settings)
    engine = ExecutionEngine(settings, repo, DummyClob(), risk, lambda tid: None)

    leg_a = LegIntent(
        token_id="A",
        outcome="UP",
        side="BUY",
        price=Decimal("0.4"),
        size=Decimal("10"),
        order_type="FOK",
    )
    leg_b = LegIntent(
        token_id="B",
        outcome="DOWN",
        side="BUY",
        price=Decimal("0.5"),
        size=Decimal("10"),
        order_type="FOK",
    )
    ctx = CycleContext(cycle_id="c2", condition_id="cond", mode="FULLSET", legs=(leg_a, leg_b))
    engine.cycles["c2"] = ctx
    engine.order_to_cycle["o1"] = "c2"
    engine.order_to_cycle["o2"] = "c2"

    await engine.handle_order_event({"order_id": "o1", "asset_id": "A", "size_matched": "5"})
    await asyncio.sleep(0.05)
    assert "FLATTENED" in repo.closed_states
