from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

import websockets
from websockets.exceptions import ConnectionClosed

from app.config import WS_MARKET
from app.model.orderbook import Orderbook

logger = logging.getLogger(__name__)


class MarketWS:
    """True WebSocket connection to Polymarket market data stream."""

    def __init__(self, reconnect_delay: float = 3.0) -> None:
        self._subscriptions: set[str] = set()
        self._orderbooks: dict[str, Orderbook] = {}
        self._on_new_market: Callable[[dict], None] | None = None
        self._reconnect_delay = reconnect_delay
        self._ws: websockets.WebSocketClientProtocol | None = None

    def set_subscriptions(self, token_ids: set[str]) -> None:
        old_subs = self._subscriptions
        self._subscriptions = set(token_ids)
        # If we have an active connection and subscriptions changed, resubscribe
        if self._ws and self._subscriptions != old_subs:
            asyncio.create_task(self._send_subscription())

    def get_orderbook(self, token_id: str) -> Orderbook | None:
        return self._orderbooks.get(token_id)

    def set_new_market_handler(self, handler: Callable[[dict], None]) -> None:
        self._on_new_market = handler

    async def _send_subscription(self) -> None:
        if not self._ws or not self._subscriptions:
            return
        try:
            msg = {"assets_ids": list(self._subscriptions), "type": "market"}
            await self._ws.send(json.dumps(msg))
            logger.info("Sent market subscription for %d tokens", len(self._subscriptions))
        except Exception as e:
            logger.warning("Failed to send subscription: %s", e)

    async def run(self, on_book_update: Callable[[str, Orderbook], None]) -> None:
        """Connect to WebSocket and process market updates with auto-reconnect."""
        while True:
            try:
                await self._connect_and_listen(on_book_update)
            except ConnectionClosed as e:
                logger.warning("WebSocket connection closed: %s, reconnecting...", e)
            except Exception as e:
                logger.error("WebSocket error: %s, reconnecting...", e)

            self._ws = None
            await asyncio.sleep(self._reconnect_delay)

    async def _connect_and_listen(self, on_book_update: Callable[[str, Orderbook], None]) -> None:
        """Establish connection and process messages."""
        async with websockets.connect(
            WS_MARKET,
            ping_interval=10,
            ping_timeout=20,  # Reduced from 30 to detect dead connections faster
            close_timeout=5,
        ) as ws:
            self._ws = ws
            logger.info("Connected to market WebSocket: %s", WS_MARKET)

            # Send initial subscription
            await self._send_subscription()

            # Track consecutive errors for reconnection
            consecutive_errors = 0
            max_consecutive_errors = 5
            last_valid_message_time = time.time()
            stale_connection_timeout = 60  # Reconnect if no valid message for 60s

            # Process incoming messages with timeout
            while True:
                try:
                    # Wait for message with timeout to detect dead connections
                    raw_message = await asyncio.wait_for(
                        ws.recv(),
                        timeout=stale_connection_timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning("No messages for %ds, forcing reconnect", stale_connection_timeout)
                    break

                try:
                    messages = json.loads(raw_message)
                    # API returns list of events
                    if not isinstance(messages, list):
                        messages = [messages]

                    for msg in messages:
                        await self._handle_message(msg, on_book_update)

                    # Reset error counter on success
                    consecutive_errors = 0
                    last_valid_message_time = time.time()

                except json.JSONDecodeError as e:
                    consecutive_errors += 1
                    logger.warning("Invalid JSON received (%d/%d): %s", consecutive_errors, max_consecutive_errors, e)
                    if consecutive_errors >= max_consecutive_errors:
                        logger.error("Too many consecutive JSON errors, forcing reconnect")
                        break
                except Exception as e:
                    consecutive_errors += 1
                    logger.warning("Error processing message (%d/%d): %s", consecutive_errors, max_consecutive_errors, e)
                    if consecutive_errors >= max_consecutive_errors:
                        logger.error("Too many consecutive errors, forcing reconnect")
                        break

    async def _handle_message(
        self, msg: dict, on_book_update: Callable[[str, Orderbook], None]
    ) -> None:
        """Handle a single WebSocket message."""
        event_type = msg.get("event_type")

        # Get token ID from message (different fields for different events)
        token_id = msg.get("asset_id") or msg.get("market")
        if not token_id:
            return

        # Ensure orderbook exists for this token
        if token_id not in self._orderbooks:
            self._orderbooks[token_id] = Orderbook(token_id=token_id)

        ob = self._orderbooks[token_id]

        if event_type == "book":
            # Full orderbook snapshot
            ob.update_from_book(msg)
            on_book_update(token_id, ob)

        elif event_type == "price_change":
            # Incremental price update
            if ob.apply_price_change(msg):
                on_book_update(token_id, ob)

        elif event_type == "tick_size_change":
            # Tick size update
            ob.apply_tick_size_change(msg)

        elif event_type == "new_market":
            # New market discovered
            if self._on_new_market:
                self._on_new_market(msg)
