"""
Polymarket Dual Price Feed Client

Four data sources:
1. Chainlink Price (via RTDS) - The oracle's authoritative price (~1s latency)
2. RTDS Binance (via RTDS) - Polymarket's relay of Binance prices
3. Direct Binance (WebSocket) - Direct exchange price (~50ms latency)
4. Price to Beat (via Gamma API) - Anchor price at round start

By comparing RTDS Binance vs Direct Binance latency, we can measure
Polymarket's system lag.

WebSocket endpoints:
- RTDS: wss://ws-live-data.polymarket.com (Chainlink + Binance relay)
- Binance: wss://stream.binance.com:9443/ws (direct)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from decimal import Decimal
from typing import Any

import httpx
import websockets

logger = logging.getLogger(__name__)

# Asset configuration
ASSETS = ["BTC", "ETH", "SOL", "XRP"]

# Polymarket WebSocket endpoints
RTDS_URL = "wss://ws-live-data.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# Binance direct WebSocket (faster than RTDS relay)
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"
BINANCE_SYMBOLS = ["btcusdt", "ethusdt", "solusdt", "xrpusdt"]

# NTP servers for clock sync
NTP_SERVERS = ["pool.ntp.org", "time.google.com", "time.cloudflare.com"]


class ClockSync:
    """
    NTP-based clock synchronization for accurate latency measurement.

    Calculates offset between local clock and NTP server to ensure
    millisecond-accurate latency calculations.
    """

    def __init__(self):
        self._offset_ms: float = 0.0  # Local clock is ahead by this amount
        self._last_sync: float = 0.0
        self._sync_interval: float = 300.0  # Re-sync every 5 minutes

    async def sync(self) -> None:
        """Synchronize with NTP server."""
        try:
            import ntplib
            client = ntplib.NTPClient()

            # Try multiple servers
            for server in NTP_SERVERS:
                try:
                    response = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: client.request(server, version=3)
                    )
                    self._offset_ms = response.offset * 1000  # Convert to ms
                    self._last_sync = time.time()
                    logger.info("NTP sync: offset=%.1fms (server=%s)", self._offset_ms, server)
                    return
                except Exception as e:
                    logger.debug("NTP server %s failed: %s", server, e)
                    continue

            logger.warning("All NTP servers failed, using local clock")

        except ImportError:
            logger.warning("ntplib not installed, using local clock")
        except Exception as e:
            logger.warning("NTP sync failed: %s", e)

    def now_ms(self) -> int:
        """Get current time in milliseconds, adjusted for NTP offset."""
        # Check if we need to re-sync
        if time.time() - self._last_sync > self._sync_interval:
            # Schedule async sync (don't block)
            asyncio.create_task(self.sync())

        return int((time.time() * 1000) - self._offset_ms)

    def latency_ms(self, remote_ts_ms: int) -> int:
        """Calculate latency from remote timestamp."""
        if not remote_ts_ms:
            return 0
        return max(0, self.now_ms() - remote_ts_ms)


class PolymarketPriceFeed:
    """
    Multi-source price feed with NTP-synchronized latency tracking.

    Provides:
    - chainlink: Chainlink oracle price (authoritative)
    - rtds_binance: RTDS-relayed Binance price (measures Polymarket lag)
    - binance: Direct Binance price (fastest)
    - anchor: Price to beat at round start
    """

    def __init__(self) -> None:
        self._running = False
        self._clock = ClockSync()

        # Chainlink prices from RTDS (crypto_prices_chainlink)
        self._chainlink: dict[str, dict] = {
            asset: {"price": Decimal("0"), "timestamp_ms": 0}
            for asset in ASSETS
        }

        # RTDS Binance prices (crypto_prices) - measures Polymarket system lag
        self._rtds_binance: dict[str, dict] = {
            asset: {"price": Decimal("0"), "timestamp_ms": 0}
            for asset in ASSETS
        }

        # Direct Binance prices (fastest)
        self._binance: dict[str, dict] = {
            asset: {"price": Decimal("0"), "timestamp_ms": 0}
            for asset in ASSETS
        }

        # Anchor prices (price to beat) from Gamma API
        self._anchor: dict[str, dict] = {
            asset: {"price": Decimal("0"), "timestamp_ms": 0, "source": ""}
            for asset in ASSETS
        }

        # Track current round
        self._current_window: int = 0

        # Tasks
        self._rtds_task: asyncio.Task | None = None
        self._binance_task: asyncio.Task | None = None
        self._anchor_task: asyncio.Task | None = None
        self._http: httpx.AsyncClient | None = None

    async def start(self) -> None:
        """Start all price feeds."""
        if self._running:
            return
        self._running = True
        self._http = httpx.AsyncClient(timeout=10)

        # Sync clock first
        await self._clock.sync()

        # Start RTDS WebSocket for Chainlink + RTDS Binance prices
        self._rtds_task = asyncio.create_task(self._rtds_loop())

        # Start direct Binance WebSocket (faster)
        self._binance_task = asyncio.create_task(self._binance_loop())

        # Start anchor price fetcher
        self._anchor_task = asyncio.create_task(self._anchor_loop())

        logger.info("Price feed started (NTP synced, RTDS + Direct Binance)")

    async def stop(self) -> None:
        """Stop all feeds."""
        self._running = False
        for task in [self._rtds_task, self._binance_task, self._anchor_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._http:
            await self._http.aclose()

    async def _rtds_loop(self) -> None:
        """Connect to RTDS WebSocket for Chainlink + RTDS Binance prices."""
        while self._running:
            try:
                async with websockets.connect(RTDS_URL, ping_interval=5) as ws:
                    logger.info("Connected to RTDS: %s", RTDS_URL)

                    # Subscribe to Chainlink prices
                    chainlink_sub = {
                        "action": "subscribe",
                        "subscriptions": [{
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": ""
                        }]
                    }
                    await ws.send(json.dumps(chainlink_sub))
                    logger.info("Subscribed to crypto_prices_chainlink")

                    # Subscribe to RTDS Binance prices (for lag comparison)
                    binance_sub = {
                        "action": "subscribe",
                        "subscriptions": [{
                            "topic": "crypto_prices",
                            "type": "*"  # Must use "*" not "update"
                        }]
                    }
                    await ws.send(json.dumps(binance_sub))
                    logger.info("Subscribed to crypto_prices (RTDS Binance)")

                    # Process messages with timeout to detect dead connections
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            await self._handle_rtds_message(msg)
                        except asyncio.TimeoutError:
                            logger.warning("RTDS no messages for 30s, reconnecting...")
                            break
                        except Exception as e:
                            logger.debug("RTDS message error: %s", e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("RTDS connection error: %s, reconnecting...", e)
                await asyncio.sleep(3)

    async def _handle_rtds_message(self, msg: str) -> None:
        """Handle RTDS WebSocket message (Chainlink + RTDS Binance)."""
        data = json.loads(msg)
        topic = data.get("topic", "")
        payload = data.get("payload", {})
        msg_timestamp = data.get("timestamp", 0)

        if topic == "crypto_prices_chainlink":
            # Chainlink format: symbol = "btc/usd"
            symbol = payload.get("symbol", "").lower()
            price = payload.get("value", 0)
            ts = payload.get("timestamp", msg_timestamp)

            for asset in ASSETS:
                if symbol == f"{asset.lower()}/usd":
                    now_ms = self._clock.now_ms()
                    self._chainlink[asset] = {
                        "price": Decimal(str(price)),
                        "timestamp_ms": ts,
                        "recv_ms": now_ms,
                    }
                    logger.debug("Chainlink %s: $%.2f latency=%dms",
                                asset, price, now_ms - ts if ts else 0)
                    break

        elif topic == "crypto_prices":
            # RTDS Binance format: symbol = "btcusdt"
            symbol = payload.get("symbol", "").lower()
            price = payload.get("value", 0)
            ts = payload.get("timestamp", msg_timestamp)

            for asset in ASSETS:
                if symbol == f"{asset.lower()}usdt":
                    now_ms = self._clock.now_ms()
                    self._rtds_binance[asset] = {
                        "price": Decimal(str(price)),
                        "timestamp_ms": ts,
                        "recv_ms": now_ms,
                    }
                    logger.debug("RTDS Binance %s: $%.2f latency=%dms",
                                asset, price, now_ms - ts if ts else 0)
                    break

    async def _binance_loop(self) -> None:
        """Connect to Binance WebSocket for fast price updates."""
        # Build stream URL for combined streams
        streams = "/".join([f"{s}@trade" for s in BINANCE_SYMBOLS])
        url = f"{BINANCE_WS_URL}/{streams}"

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info("Connected to Binance Direct: %s", url)

                    # Process messages with timeout
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=10)
                            await self._handle_binance_message(msg)
                        except asyncio.TimeoutError:
                            logger.warning("Binance no messages for 10s, reconnecting...")
                            break
                        except Exception as e:
                            logger.debug("Binance message error: %s", e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Binance WebSocket error: %s, reconnecting...", e)
                await asyncio.sleep(2)

    async def _handle_binance_message(self, msg: str) -> None:
        """Handle Binance trade message."""
        data = json.loads(msg)

        # Trade message format: {"e":"trade","E":timestamp,"s":"BTCUSDT","p":"price",...}
        if data.get("e") != "trade":
            return

        symbol = data.get("s", "").lower()  # e.g., "btcusdt"
        price = data.get("p", "0")  # Price as string
        event_time = data.get("E", 0)  # Event time in ms

        for asset in ASSETS:
            if symbol == f"{asset.lower()}usdt":
                now_ms = self._clock.now_ms()
                self._binance[asset] = {
                    "price": Decimal(price),
                    "timestamp_ms": event_time,
                    "recv_ms": now_ms,
                }
                logger.debug("Binance Direct %s: $%s latency=%dms",
                            asset, price, now_ms - event_time if event_time else 0)
                break

    async def _anchor_loop(self) -> None:
        """Fetch anchor prices (price to beat) from Gamma API."""
        retry_count = 0
        while self._running:
            try:
                now = int(time.time())
                window = (now // 900) * 900

                # Check if new round started
                if window != self._current_window:
                    self._current_window = window
                    retry_count = 0
                    logger.info("New round: %d, fetching anchor prices...", window)
                    await self._fetch_anchor_prices(window)

                # Retry fetching anchor if any are missing (e.g., after restart)
                missing = [a for a in ASSETS if self._anchor[a].get("price", Decimal("0")) == 0]
                if missing and retry_count < 10:
                    retry_count += 1
                    logger.info("Retrying anchor fetch for %s (attempt %d)...", missing, retry_count)
                    await asyncio.sleep(2)  # Wait for price feeds to connect
                    await self._fetch_anchor_prices(window)

            except Exception as e:
                logger.warning("Anchor fetch error: %s", e)

            await asyncio.sleep(1.0)

    async def _fetch_anchor_prices(self, window: int) -> None:
        """Fetch anchor prices from Gamma API market descriptions."""
        if not self._http:
            return

        for asset in ASSETS:
            slug = f"{asset.lower()}-updown-15m-{window}"
            try:
                resp = await self._http.get(
                    f"{GAMMA_API}/markets",
                    params={"slug": slug}
                )

                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        market = data[0]

                        # Try to extract price from description or question
                        desc = market.get("description", "")
                        question = market.get("question", "")

                        # Look for price patterns like $93,456.78
                        prices = re.findall(r'\$([0-9,]+(?:\.[0-9]+)?)', desc + " " + question)

                        if prices:
                            # Use first found price
                            price_str = prices[0].replace(",", "")
                            self._anchor[asset] = {
                                "price": Decimal(price_str),
                                "timestamp_ms": int(time.time() * 1000),
                                "source": "description",
                            }
                            logger.info("Anchor %s: $%s (from description)", asset, price_str)
                        else:
                            # If no price in description, use current Chainlink price as anchor
                            chainlink_price = self._chainlink[asset].get("price", Decimal("0"))
                            binance_price = self._binance[asset].get("price", Decimal("0"))

                            if chainlink_price > 0:
                                self._anchor[asset] = {
                                    "price": chainlink_price,
                                    "timestamp_ms": int(time.time() * 1000),
                                    "source": "chainlink_snapshot",
                                }
                                logger.info("Anchor %s: $%.2f (from Chainlink snapshot)",
                                           asset, float(chainlink_price))
                            elif binance_price > 0:
                                # Fallback to Binance if Chainlink not available yet
                                self._anchor[asset] = {
                                    "price": binance_price,
                                    "timestamp_ms": int(time.time() * 1000),
                                    "source": "binance_snapshot",
                                }
                                logger.info("Anchor %s: $%.2f (from Binance snapshot)",
                                           asset, float(binance_price))

            except Exception as e:
                logger.warning("Failed to fetch anchor for %s: %s", asset, e)

    def snapshot(self) -> dict[str, Any]:
        """Get snapshot of all price data with latency info."""
        result = {}
        now_ms = self._clock.now_ms()

        for asset in ASSETS:
            chainlink = self._chainlink.get(asset, {})
            rtds_binance = self._rtds_binance.get(asset, {})
            binance = self._binance.get(asset, {})
            anchor = self._anchor.get(asset, {})

            chainlink_price = chainlink.get("price", Decimal("0"))
            anchor_price = anchor.get("price", Decimal("0"))

            # Calculate change from anchor
            change = chainlink_price - anchor_price if anchor_price > 0 else Decimal("0")
            change_pct = (change / anchor_price * 100) if anchor_price > 0 else Decimal("0")

            # Calculate real-time age (how stale the data is)
            chainlink_ts = chainlink.get("timestamp_ms", 0)
            rtds_binance_ts = rtds_binance.get("timestamp_ms", 0)
            binance_ts = binance.get("timestamp_ms", 0)

            chainlink_age = now_ms - chainlink_ts if chainlink_ts else 0
            rtds_binance_age = now_ms - rtds_binance_ts if rtds_binance_ts else 0
            binance_age = now_ms - binance_ts if binance_ts else 0

            # Calculate Polymarket system lag (RTDS Binance vs Direct Binance)
            # This shows how much delay Polymarket adds to their feed
            polymarket_lag = rtds_binance_age - binance_age if (rtds_binance_ts and binance_ts) else 0

            result[asset] = {
                # Chainlink oracle price (authoritative for settlement)
                "chainlink": {
                    "price": float(chainlink.get("price", 0)),
                    "timestamp_ms": chainlink_ts,
                    "latency_ms": chainlink_age,
                    "age_ms": chainlink_age,
                },
                # RTDS Binance (measures Polymarket system lag)
                "rtds_binance": {
                    "price": float(rtds_binance.get("price", 0)),
                    "timestamp_ms": rtds_binance_ts,
                    "latency_ms": rtds_binance_age,
                    "age_ms": rtds_binance_age,
                },
                # Direct Binance (fastest reference)
                "binance": {
                    "price": float(binance.get("price", 0)),
                    "timestamp_ms": binance_ts,
                    "latency_ms": binance_age,
                    "age_ms": binance_age,
                },
                # Price to beat (anchor)
                "anchor": {
                    "price": float(anchor.get("price", 0)),
                    "timestamp_ms": anchor.get("timestamp_ms", 0),
                    "source": anchor.get("source", ""),
                },
                # Computed values
                "change": float(change),
                "change_pct": float(change_pct),
                # Polymarket system lag (RTDS delay vs direct)
                "polymarket_lag_ms": polymarket_lag,
                # Legacy compatibility
                "current": float(chainlink_price),
            }

        result["_timestamp_ms"] = now_ms
        result["_ntp_offset_ms"] = self._clock._offset_ms
        return result


# Global instance
_price_feed: PolymarketPriceFeed | None = None


def get_price_feed() -> PolymarketPriceFeed:
    """Get the global price feed instance."""
    global _price_feed
    if _price_feed is None:
        _price_feed = PolymarketPriceFeed()
    return _price_feed


async def init_price_feed() -> PolymarketPriceFeed:
    """Initialize and start the price feed."""
    feed = get_price_feed()
    await feed.start()
    return feed
