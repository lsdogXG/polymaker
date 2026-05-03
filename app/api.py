"""
WebSocket API server for the trading terminal frontend.
Streams real-time data: positions, market analysis, transactions, stats.
Requires token authentication via DASHBOARD_TOKEN environment variable.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import asdict
from datetime import datetime, timezone, timedelta


# New York timezone (UTC-5 for EST)
NYC_OFFSET = timedelta(hours=-5)

def utc_to_nyc(dt_str: str) -> str:
    """Convert UTC ISO timestamp string to NYC time string."""
    if not dt_str:
        return ""
    try:
        # Parse the UTC timestamp
        if "T" in dt_str:
            dt_str_clean = dt_str[:19]  # Remove milliseconds and timezone
            utc_dt = datetime.fromisoformat(dt_str_clean).replace(tzinfo=timezone.utc)
        else:
            utc_dt = datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        # Convert to NYC time
        nyc_dt = utc_dt + NYC_OFFSET
        return nyc_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return dt_str[:19].replace("T", " ") if dt_str else ""

def now_nyc() -> str:
    """Get current NYC time as formatted string."""
    utc_now = datetime.now(timezone.utc)
    nyc_now = utc_now + NYC_OFFSET
    return nyc_now.strftime("%Y-%m-%d %H:%M:%S")
from decimal import Decimal
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from app.model.orderbook import Orderbook


# Polymarket 15-min market taker fee calculation
# Fee = baseFee * (1 - |price - 0.5| * 2)
# where baseFee = 2% for 15-min markets
BASE_TAKER_FEE = Decimal("0.02")  # 2%


def calculate_taker_fee(price: Decimal) -> Decimal:
    """Calculate taker fee based on outcome price.

    Fee decreases as price moves away from 0.5:
    - At 0.50: 2.0%
    - At 0.30/0.70: 1.2%
    - At 0.10/0.90: 0.4%
    - At 0.05/0.95: 0.2%
    """
    if price <= 0 or price >= 1:
        return Decimal("0")
    distance = abs(price - Decimal("0.5")) * 2  # 0 to 1
    return BASE_TAKER_FEE * (1 - distance)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import uvicorn

logger = logging.getLogger(__name__)

# Authentication token from environment variable
# Generate one if not set: python -c "import secrets; print(secrets.token_urlsafe(32))"
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")

security = HTTPBearer(auto_error=False)


def verify_token(token: str) -> bool:
    """Verify the provided token against DASHBOARD_TOKEN."""
    if not DASHBOARD_TOKEN:
        logger.warning("DASHBOARD_TOKEN not set - authentication disabled!")
        return True  # Allow access if no token is configured (dev mode)
    return secrets.compare_digest(token, DASHBOARD_TOKEN)


async def get_current_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """Dependency for REST API authentication."""
    if not DASHBOARD_TOKEN:
        return ""  # No auth required if token not set
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not verify_token(credentials.credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials


def verify_ws_token(token: str | None) -> bool:
    """Verify WebSocket token from query parameter."""
    if not DASHBOARD_TOKEN:
        return True  # No auth required if token not set
    if not token:
        return False
    return secrets.compare_digest(token, DASHBOARD_TOKEN)


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, cls=DecimalEncoder)


class DashboardState:
    """Aggregates data from coordinator/executor for dashboard display."""

    SUPPORTED_ASSETS = ["BTC", "ETH", "SOL", "XRP"]

    def __init__(self) -> None:
        self.positions: dict[str, dict] = {
            "UP": {"qty": Decimal("0"), "avg_price": Decimal("0"), "cost": Decimal("0"), "pnl": Decimal("0")},
            "DOWN": {"qty": Decimal("0"), "avg_price": Decimal("0"), "cost": Decimal("0"), "pnl": Decimal("0")},
        }
        # Multi-market data structure
        self.markets: dict[str, dict[str, Any]] = {
            asset: self._empty_market_data() for asset in self.SUPPORTED_ASSETS
        }
        # Legacy single market for backward compatibility
        self.market: dict[str, Any] = {
            "up_price": Decimal("0"),
            "down_price": Decimal("0"),
            "combined": Decimal("0"),
            "spread": Decimal("0"),
            "pairs": 0,
            "delta": Decimal("0"),
            "total_pnl": Decimal("0"),
        }
        # Orderbook depth for visualization
        self.orderbooks: dict[str, dict[str, list]] = {
            asset: {"yes_bids": [], "yes_asks": [], "no_bids": [], "no_asks": []}
            for asset in self.SUPPORTED_ASSETS
        }
        self.transactions: list[dict] = []
        # Strategy signals / intents
        self.signals: list[dict] = []
        self.stats: dict[str, Any] = {
            "trades": 0,
            "volume": Decimal("0"),
            "wallet": "0x...",
            "usdc_balance": Decimal("0"),
            "uptime": 0,
            "book_updates": 0,
            "order_events": 0,
            "intents_fullset": 0,
            "intents_single_leg": 0,
            "active_cycles": 0,
            "markets_count": 0,
        }
        # Real-time crypto prices from Binance
        self.prices: dict[str, dict[str, Any]] = {
            asset: {"current": 0.0, "anchor": 0.0, "change": 0.0, "change_pct": 0.0}
            for asset in self.SUPPORTED_ASSETS
        }
        self._start_time = time.time()
        self._last_book_update: dict[str, float] = {asset: 0.0 for asset in self.SUPPORTED_ASSETS}
        self._price_feed = None  # Will be set via set_price_feed()

    def set_price_feed(self, price_feed) -> None:
        """Set the price feed for real-time price updates."""
        self._price_feed = price_feed

    def _empty_market_data(self) -> dict[str, Any]:
        """Create empty market data structure."""
        return {
            "active": False,
            "slug": "",
            "condition_id": "",
            "yes_price": Decimal("0"),
            "no_price": Decimal("0"),
            "yes_bid": Decimal("0"),
            "yes_ask": Decimal("0"),
            "no_bid": Decimal("0"),
            "no_ask": Decimal("0"),
            "combined": Decimal("0"),
            "spread": Decimal("0"),  # 1 - combined (arb opportunity)
            "arb_pct": Decimal("0"),  # spread as percentage
            "last_update": 0.0,
            "book_age_ms": 0,
            "round_locked": False,  # True when round is ending soon (last 30s)
            "remaining_sec": 0.0,   # Seconds remaining in round
            # Depth analysis for $1000 trade
            "yes_vwap_1000": Decimal("0"),  # VWAP to buy $1000 worth of YES
            "no_vwap_1000": Decimal("0"),   # VWAP to buy $1000 worth of NO
            "yes_depth": [],  # Top 5 ask levels [(price, size), ...]
            "no_depth": [],   # Top 5 ask levels
            # Fee calculation
            "yes_fee_pct": Decimal("0"),  # Taker fee for YES at current price
            "no_fee_pct": Decimal("0"),   # Taker fee for NO
            "total_fee_pct": Decimal("0"),  # Combined fee for fullset
            # Net ARB after fees
            "net_arb_pct": Decimal("0"),  # arb_pct - total_fee_pct
        }

    def snapshot(self) -> dict[str, Any]:
        self.stats["uptime"] = int(time.time() - self._start_time)
        now = time.time()

        # Calculate round timing
        window_start = int(now // 900) * 900
        window_end = window_start + 900
        remaining_sec = window_end - now
        round_locked = remaining_sec <= 30.0

        # Update book age for all markets
        markets_snapshot = {}
        for asset, mkt in self.markets.items():
            mkt_copy = {k: float(v) if isinstance(v, Decimal) else v for k, v in mkt.items()}
            # Calculate book age in milliseconds
            if mkt["last_update"] > 0:
                mkt_copy["book_age_ms"] = int((now - mkt["last_update"]) * 1000)
            # Add round lock status
            mkt_copy["round_locked"] = round_locked
            mkt_copy["remaining_sec"] = remaining_sec
            markets_snapshot[asset] = mkt_copy

        # Get price data from price feed
        prices_snapshot = {}
        if self._price_feed:
            prices_snapshot = self._price_feed.snapshot()
        else:
            prices_snapshot = self.prices.copy()

        return {
            # Round timing info
            "round": {
                "window_start": window_start,
                "window_end": window_end,
                "remaining_sec": remaining_sec,
                "locked": round_locked,
            },
            "positions": {
                k: {kk: float(vv) if isinstance(vv, Decimal) else vv for kk, vv in v.items()}
                for k, v in self.positions.items()
            },
            # Multi-market data (new)
            "markets": markets_snapshot,
            # Real-time crypto prices
            "prices": prices_snapshot,
            # Orderbook depth for visualization
            "orderbooks": {
                asset: {
                    side: [[float(p), float(s)] for p, s in levels[:10]]  # Top 10 levels
                    for side, levels in ob.items()
                }
                for asset, ob in self.orderbooks.items()
            },
            # Strategy signals
            "signals": self.signals[-20:],  # Last 20 signals
            # Legacy single market for backward compatibility
            "market": {
                k: float(v) if isinstance(v, Decimal) else v
                for k, v in self.market.items()
            },
            "transactions": self.transactions[-50:],  # Last 50 transactions
            "stats": {
                k: float(v) if isinstance(v, Decimal) else v
                for k, v in self.stats.items()
            },
            "timestamp": now_nyc(),
        }


class ConnectionManager:
    """Manages WebSocket connections for broadcasting updates."""

    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info("Dashboard client connected. Total: %d", len(self.active_connections))

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info("Dashboard client disconnected. Total: %d", len(self.active_connections))

    async def broadcast(self, data: dict) -> None:
        if not self.active_connections:
            return
        message = json_dumps(data)
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                disconnected.append(connection)
        for conn in disconnected:
            self.disconnect(conn)


# Global state
dashboard_state = DashboardState()
manager = ConnectionManager()
app = FastAPI(title="Polymarket Arb Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(default="")) -> None:
    """
    WebSocket endpoint for real-time dashboard data.
    Requires token query parameter: ws://host:port/ws?token=YOUR_TOKEN
    """
    # Verify token before accepting connection
    if not verify_ws_token(token):
        logger.warning("WebSocket connection rejected: invalid token from %s", websocket.client)
        await websocket.close(code=4001, reason="Invalid or missing authentication token")
        return

    await manager.connect(websocket)
    logger.info("WebSocket authenticated and connected from %s", websocket.client)
    try:
        # Send initial state
        await websocket.send_text(json_dumps(dashboard_state.snapshot()))
        while True:
            # Keep connection alive, actual updates come from broadcast
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                # Send ping/state update
                await websocket.send_text(json_dumps({"type": "ping", "ts": time.time()}))
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.warning("WebSocket error: %s", e)
        manager.disconnect(websocket)


@app.get("/api/state")
async def get_state(token: str = Depends(get_current_token)) -> dict:
    """Get current dashboard state. Requires Bearer token authentication."""
    return dashboard_state.snapshot()


@app.get("/api/health")
async def health() -> dict:
    """Health check endpoint (no auth required)."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/auth/verify")
async def verify_auth(token: str = Depends(get_current_token)) -> dict:
    """Verify authentication token is valid."""
    return {"status": "ok", "authenticated": True}


class DashboardBridge:
    """
    Bridge between the trading bot components and the dashboard.
    Integrates with Coordinator, ExecutionEngine, RuntimeStats, and MongoDB.
    Also fetches positions from Polymarket Data API.
    """

    def __init__(
        self,
        state: DashboardState,
        conn_manager: ConnectionManager,
    ) -> None:
        self.state = state
        self.manager = conn_manager
        self._broadcast_task: asyncio.Task | None = None
        self._db_sync_task: asyncio.Task | None = None
        self._positions_task: asyncio.Task | None = None
        self._repo = None  # Will be set via set_repo()
        self._clob = None  # Will be set via set_clob()
        self._wallet_address: str = ""

    def set_repo(self, repo) -> None:
        """Set the database repository for fetching data."""
        self._repo = repo

    def set_clob(self, clob) -> None:
        """Set the CLOB client for balance fetching."""
        self._clob = clob

    def start(self) -> None:
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())
        self._db_sync_task = asyncio.create_task(self._db_sync_loop())
        self._positions_task = asyncio.create_task(self._positions_sync_loop())

    async def _broadcast_loop(self) -> None:
        """Broadcast state updates at 50ms intervals."""
        while True:
            try:
                await self.manager.broadcast(self.state.snapshot())
                await asyncio.sleep(0.05)  # 50ms = 20 updates/sec
            except Exception as e:
                logger.warning("Broadcast error: %s", e)
                await asyncio.sleep(1)

    async def _db_sync_loop(self) -> None:
        """Sync data from MongoDB every 50ms."""
        while True:
            try:
                if self._repo:
                    await self._sync_from_db()
            except Exception as e:
                logger.warning("DB sync error: %s", e)
            await asyncio.sleep(0.05)  # 50ms = 20 syncs/sec

    async def _positions_sync_loop(self) -> None:
        """Fetch balance and trades from CLOB client every 5 seconds."""
        while True:
            try:
                await self._fetch_balance_from_clob()
                await self._fetch_trades_from_clob()
            except Exception as e:
                logger.warning("CLOB sync error: %s", e)
            await asyncio.sleep(5.0)  # Fetch every 5 seconds

    async def _fetch_balance_from_clob(self) -> None:
        """Fetch USDC balance from CLOB client."""
        if not self._clob:
            return

        try:
            # Run in executor since CLOB client is synchronous
            loop = asyncio.get_event_loop()
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = await loop.run_in_executor(
                None, self._clob.client.get_balance_allowance, params
            )
            if result and isinstance(result, dict):
                balance = result.get("balance", 0)
                # Balance is in wei (6 decimals for USDC)
                balance_decimal = Decimal(str(balance)) / Decimal("1000000")
                self.state.stats["usdc_balance"] = balance_decimal
                logger.info("CLOB USDC balance: %s", balance_decimal)
        except Exception as e:
            logger.warning("Failed to fetch balance from CLOB: %s", e)

    async def _fetch_trades_from_clob(self) -> None:
        """Fetch recent trades from CLOB client."""
        if not self._clob:
            return

        try:
            # Run in executor since CLOB client is synchronous
            loop = asyncio.get_event_loop()
            trades = await loop.run_in_executor(
                None, self._clob.client.get_trades, None, "MA=="
            )

            if trades:
                transactions = []
                for trade in trades[:50]:  # Limit to 50
                    # Parse timestamp
                    ts = trade.get("matchTime") or trade.get("createdAt") or ""
                    time_str = utc_to_nyc(ts) if ts else ""

                    tx = {
                        "time": time_str,
                        "side": trade.get("side", "").upper(),
                        "price": float(trade.get("price", 0) or 0),
                        "size": float(trade.get("size", 0) or 0),
                        "usdcSize": float(trade.get("size", 0) or 0) * float(trade.get("price", 0) or 0),
                        "type": "TRADE",
                        "outcome": trade.get("outcome", ""),
                        "title": trade.get("market", "") or trade.get("asset_id", "")[:20] if trade.get("asset_id") else "",
                        "tx_hash": trade.get("transactionHash", "") or trade.get("id", "") or "",
                        "status": trade.get("status", ""),
                    }
                    transactions.append(tx)

                if transactions:
                    self.state.transactions = transactions
                    logger.info("Fetched %d trades from CLOB", len(transactions))
        except Exception as e:
            logger.warning("Failed to fetch trades from CLOB: %s", e)


    async def _sync_from_db(self) -> None:
        """Fetch latest data from MongoDB and update state."""
        if not self._repo:
            return

        # Fetch recent trades for transactions list
        try:
            trades = await self._repo.get_recent_trades(limit=50)
            if trades:
                transactions = []
                for trade in trades:
                    tx = {
                        "time": utc_to_nyc(trade.get("updated_at", "")),
                        "side": "UP" if str(trade.get("side", "")).upper() == "BUY" else "DOWN",
                        "price": float(trade.get("price", 0)),
                        "size": float(trade.get("size", 0)),
                        "underlying": None,
                        "tx_hash": trade.get("transactionHash", trade.get("tx_hash", "")) or "",
                    }
                    transactions.append(tx)
                self.state.transactions = transactions
            else:
                # If no trades, show recent cycles (intents) instead
                cycles = await self._repo.get_recent_cycles(limit=50)
                transactions = []
                for cycle in cycles:
                    params = cycle.get("params", {})
                    limits = params.get("limits", {})
                    ts = cycle.get("timestamps", {}).get("created_at", "")
                    time_str = utc_to_nyc(ts)
                    # Show UP and DOWN legs from the cycle
                    for side, price in limits.items():
                        tx = {
                            "time": time_str,
                            "side": side.upper(),
                            "price": float(price) if price else 0,
                            "size": float(params.get("target_shares", 0)),
                            "underlying": None,
                            "tx_hash": f"{cycle.get('state', 'UNKNOWN')}:{cycle.get('cycle_id', '')[:8]}",
                        }
                        transactions.append(tx)
                self.state.transactions = transactions[:100]  # Limit to 100
        except Exception as e:
            logger.debug("Failed to fetch trades/cycles: %s", e)

        # Fetch position summary
        try:
            positions = await self._repo.get_position_summary()
            for side_key, side_name in [("BUY", "UP"), ("SELL", "DOWN")]:
                if side_key in positions:
                    pos = positions[side_key]
                    self.state.positions[side_name] = {
                        "qty": Decimal(str(pos.get("qty", 0))),
                        "avg_price": Decimal(str(pos.get("avg_price", 0))),
                        "cost": Decimal(str(pos.get("cost", 0))),
                        "pnl": Decimal("0"),  # Will be calculated with current price
                    }
        except Exception as e:
            logger.debug("Failed to fetch positions: %s", e)

        # Fetch dashboard stats
        try:
            db_stats = await self._repo.get_dashboard_stats()
            self.state.stats["trades"] = db_stats.get("trades_count", 0)
            self.state.stats["volume"] = Decimal(str(db_stats.get("total_volume", 0)))
            self.state.stats["active_cycles"] = db_stats.get("active_cycles", 0)

            # Also get cycle stats
            cycle_stats = await self._repo.get_cycle_stats()
            intents_count = sum(s.get("count", 0) for s in cycle_stats.values())
            self.state.stats["intents_fullset"] = intents_count
        except Exception as e:
            logger.debug("Failed to fetch stats: %s", e)

    def update_from_coordinator(
        self,
        markets: dict,
        condition_tokens: dict,
        executor_cycles: dict,
    ) -> None:
        """Update state from coordinator data."""
        self.state.stats["markets_count"] = len(markets)
        self.state.stats["active_cycles"] = len(executor_cycles)

    def update_from_stats(self, stats_snapshot: dict) -> None:
        """Update from RuntimeStats snapshot."""
        self.state.stats["book_updates"] = stats_snapshot.get("book_updates", 0)
        self.state.stats["order_events"] = stats_snapshot.get("order_events", 0)
        self.state.stats["trades"] = stats_snapshot.get("trade_events", 0)
        self.state.stats["intents_fullset"] = stats_snapshot.get("intents_fullset", 0)
        self.state.stats["intents_single_leg"] = stats_snapshot.get("intents_single_leg", 0)

    def update_orderbook(
        self,
        up_best_ask: Decimal | None,
        up_best_bid: Decimal | None,
        down_best_ask: Decimal | None,
        down_best_bid: Decimal | None,
        asset: str = "BTC",
        slug: str = "",
        condition_id: str = "",
        yes_book: list | None = None,
        no_book: list | None = None,
        ob_yes: "Orderbook | None" = None,
        ob_no: "Orderbook | None" = None,
    ) -> None:
        """Update market analysis from orderbook data with depth and fee calculations."""
        up_price = up_best_ask or Decimal("0")
        down_price = down_best_ask or Decimal("0")
        combined = up_price + down_price
        spread = Decimal("1") - combined
        arb_pct = spread * 100 if combined > 0 else Decimal("0")

        # Calculate taker fees based on current prices
        yes_fee = calculate_taker_fee(up_price) if up_price > 0 else Decimal("0")
        no_fee = calculate_taker_fee(down_price) if down_price > 0 else Decimal("0")
        total_fee_pct = (yes_fee + no_fee) * 100  # As percentage

        # Net ARB after fees
        net_arb_pct = arb_pct - total_fee_pct

        # Update legacy single market (for backward compatibility)
        self.state.market["up_price"] = up_price
        self.state.market["down_price"] = down_price
        self.state.market["combined"] = combined
        self.state.market["spread"] = spread

        # Delta = difference in bid-ask midpoints
        up_mid = ((up_best_ask or Decimal("0")) + (up_best_bid or Decimal("0"))) / 2
        down_mid = ((down_best_ask or Decimal("0")) + (down_best_bid or Decimal("0"))) / 2
        self.state.market["delta"] = up_mid - down_mid

        # Calculate VWAP for $1000 trade and extract depth
        yes_vwap = Decimal("0")
        no_vwap = Decimal("0")
        yes_depth = []
        no_depth = []

        if ob_yes:
            # Calculate VWAP cost to buy $1000 worth (need to solve for shares)
            # $1000 = shares * avg_price, so we estimate with current price first
            if up_price > 0:
                estimated_shares = Decimal("1000") / up_price
                vwap_result = ob_yes.vwap_cost("BUY", estimated_shares)
                if vwap_result:
                    yes_vwap = vwap_result / estimated_shares  # Average price
            # Extract top 5 ask levels
            yes_depth = [(float(lvl.price), float(lvl.size)) for lvl in ob_yes.asks[:5]]

        if ob_no:
            if down_price > 0:
                estimated_shares = Decimal("1000") / down_price
                vwap_result = ob_no.vwap_cost("BUY", estimated_shares)
                if vwap_result:
                    no_vwap = vwap_result / estimated_shares
            no_depth = [(float(lvl.price), float(lvl.size)) for lvl in ob_no.asks[:5]]

        # Update multi-market data
        if asset in self.state.markets:
            now = time.time()
            self.state.markets[asset].update({
                "active": True,
                "slug": slug,
                "condition_id": condition_id,
                "yes_price": up_price,
                "no_price": down_price,
                "yes_bid": up_best_bid or Decimal("0"),
                "yes_ask": up_best_ask or Decimal("0"),
                "no_bid": down_best_bid or Decimal("0"),
                "no_ask": down_best_ask or Decimal("0"),
                "combined": combined,
                "spread": spread,
                "arb_pct": arb_pct,
                "last_update": now,
                # Depth analysis
                "yes_vwap_1000": yes_vwap,
                "no_vwap_1000": no_vwap,
                "yes_depth": yes_depth,
                "no_depth": no_depth,
                # Fee calculation
                "yes_fee_pct": yes_fee * 100,
                "no_fee_pct": no_fee * 100,
                "total_fee_pct": total_fee_pct,
                "net_arb_pct": net_arb_pct,
            })
            self.state._last_book_update[asset] = now

            # Update orderbook depth (legacy format)
            if ob_yes:
                self.state.orderbooks[asset]["yes_bids"] = [
                    (lvl.price, lvl.size) for lvl in ob_yes.bids[:10]
                ]
                self.state.orderbooks[asset]["yes_asks"] = [
                    (lvl.price, lvl.size) for lvl in ob_yes.asks[:10]
                ]
            if ob_no:
                self.state.orderbooks[asset]["no_bids"] = [
                    (lvl.price, lvl.size) for lvl in ob_no.bids[:10]
                ]
                self.state.orderbooks[asset]["no_asks"] = [
                    (lvl.price, lvl.size) for lvl in ob_no.asks[:10]
                ]

    def add_signal(
        self,
        asset: str,
        signal_type: str,  # "FULLSET", "SINGLE_LEG", "SKIP"
        combined: Decimal,
        spread: Decimal,
        reason: str = "",
    ) -> None:
        """Add a strategy signal for display."""
        signal = {
            "time": now_nyc(),
            "asset": asset,
            "type": signal_type,
            "combined": float(combined),
            "spread": float(spread),
            "arb_pct": float(spread * 100),
            "reason": reason,
        }
        self.state.signals.append(signal)
        # Keep last 50 signals
        if len(self.state.signals) > 50:
            self.state.signals = self.state.signals[-50:]

    def update_positions(
        self,
        up_qty: Decimal,
        up_avg: Decimal,
        up_cost: Decimal,
        down_qty: Decimal,
        down_avg: Decimal,
        down_cost: Decimal,
        current_up_price: Decimal,
        current_down_price: Decimal,
    ) -> None:
        """Update position data."""
        # PnL = (current_price - avg_price) * qty for each side
        up_pnl = (current_up_price - up_avg) * up_qty if up_qty > 0 else Decimal("0")
        down_pnl = (current_down_price - down_avg) * down_qty if down_qty > 0 else Decimal("0")

        self.state.positions["UP"] = {
            "qty": up_qty,
            "avg_price": up_avg,
            "cost": up_cost,
            "pnl": up_pnl,
        }
        self.state.positions["DOWN"] = {
            "qty": down_qty,
            "avg_price": down_avg,
            "cost": down_cost,
            "pnl": down_pnl,
        }

        # Pairs = min of UP and DOWN quantities (hedged pairs)
        self.state.market["pairs"] = int(min(up_qty, down_qty))
        self.state.market["total_pnl"] = up_pnl + down_pnl
        self.state.stats["volume"] = up_cost + down_cost

    def add_transaction(
        self,
        timestamp: datetime,
        side: str,
        price: Decimal,
        size: Decimal,
        tx_hash: str | None = None,
        underlying_price: Decimal | None = None,
    ) -> None:
        """Add a new transaction to the list."""
        tx = {
            "time": timestamp.strftime("%H:%M:%S.%f")[:-3],
            "side": side.upper(),
            "price": float(price),
            "size": float(size),
            "underlying": float(underlying_price) if underlying_price else None,
            "tx_hash": tx_hash or "",
        }
        self.state.transactions.append(tx)
        # Keep last 100 transactions
        if len(self.state.transactions) > 100:
            self.state.transactions = self.state.transactions[-100:]

    def set_wallet(self, address: str) -> None:
        """Set wallet address for display and API fetching."""
        self.state.stats["wallet"] = address
        self._wallet_address = address
        logger.info("Dashboard wallet address set to: %s", address)


# Global bridge instance
bridge = DashboardBridge(dashboard_state, manager)


def get_bridge() -> DashboardBridge:
    return bridge


def get_app() -> FastAPI:
    return app


@app.get("/")
async def serve_frontend() -> FileResponse:
    """Serve the frontend HTML file."""
    import os
    frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")
    if os.path.exists(frontend_path):
        return FileResponse(frontend_path, media_type="text/html")
    return FileResponse("frontend/index.html", media_type="text/html")


async def run_api_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Run the API server."""
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
