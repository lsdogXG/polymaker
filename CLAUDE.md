# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Polymarket BTC 15-minute round arbitrage bot. Hybrid dual-mode strategy trading on Polymarket prediction markets via their CLOB API.

## Commands

```bash
# Setup
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

# Run
python -m app.main

# Tests
pytest                           # run all tests
pytest tests/test_sizing.py      # run single test file
pytest -k test_name              # run tests matching pattern

# Type checking
mypy app/

# Linting
ruff check app/
```

## Environment

Copy `.env.example` to `.env`. Required vars: `PRIVATE_KEY`, `FUNDER_ADDRESS`, `SIGNATURE_TYPE`. Set `DRY_RUN=1` to disable order placement.

## Architecture

### Trading Modes

- **FULLSET**: Buy both YES and NO outcomes simultaneously when combined cost < $1 (minus fees/buffer). Uses FOK orders.
- **SINGLE_LEG**: Early in new markets, buy one side at gift prices then hedge with GTD limit order.

### Core Flow

1. **Market Discovery**: `gamma.py` polls Gamma API for BTC 15-min markets matching slug patterns
2. **Orderbook Updates**: `ws_market.py` maintains real-time orderbooks via WebSocket
3. **Strategy Evaluation**: On each book update, `Coordinator.on_book_update()` evaluates fullset then single_leg strategies
4. **Execution**: `ExecutionEngine` submits orders, tracks fill status via `UserWS`, handles rescue/flatten on partial fills
5. **State Persistence**: MongoDB stores cycles, orders, trades, and audit logs

### Key Models

- `TradeIntent`: Strategy output with mode, legs, limits, expected edge
- `LegIntent`: Single order instruction (token_id, side, price, size, order_type)
- `CycleContext`: In-memory execution state tracking fills and order IDs
- `Orderbook`: Bid/ask levels with VWAP cost calculations

### Cycle States

`CREATED → SUBMITTED → PARTIAL/HEDGED → CONFIRMED/FLATTENED/RESCUED`

Rescue flow activates after `max_unhedged_sec` if partially filled.

### Risk Management

`RiskManager` enforces per-trade, per-market, and total USDC limits. Stale book detection skips trading when orderbook age exceeds `stale_book_ms`.

### External Dependencies

- `py-clob-client`: Polymarket CLOB SDK for order signing/submission
- Polymarket WebSockets for real-time market data and user order/trade events
- MongoDB for persistence

### Test Configuration

Tests use `pytest-asyncio` with auto mode. Test path is configured in `pyproject.toml` but actual tests are in `tests/`.

## Dashboard

CRT-style real-time monitoring dashboard at `frontend/index.html`.

### Setup

1. Generate auth token: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
2. Add to `.env`: `DASHBOARD_TOKEN=your_generated_token`
3. Optionally set `DASHBOARD_PORT=8080` (default)

### Access

- Run bot: `python -m app.main` (starts API server on port 8080)
- Open `frontend/index.html` in browser
- Enter token or access via URL: `?token=your_token`

### API Endpoints

- `GET /api/health` - Health check (no auth)
- `GET /api/state` - Current state (requires Bearer token)
- `GET /api/auth/verify` - Verify token validity
- `WS /ws?token=xxx` - Real-time WebSocket stream (100ms updates)

### Dashboard Data Flow

`Coordinator.on_book_update()` → `DashboardBridge.update_orderbook()` → WebSocket broadcast → Frontend render
