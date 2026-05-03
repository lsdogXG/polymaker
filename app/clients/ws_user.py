from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import websockets
from websockets.exceptions import ConnectionClosed

from app.config import WS_USER

logger = logging.getLogger(__name__)


class UserWS:
    def __init__(self, api_key: str, api_secret: str, api_passphrase: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self._subscriptions: set[str] = set()
        self._resubscribe = asyncio.Event()

    def set_subscriptions(self, condition_ids: set[str]) -> None:
        if condition_ids != self._subscriptions:
            self._subscriptions = set(condition_ids)
            self._resubscribe.set()

    async def run(
        self,
        on_order_event: Callable[[dict], None],
        on_trade_event: Callable[[dict], None],
    ) -> None:
        while True:
            try:
                async with websockets.connect(WS_USER, ping_interval=20) as ws:
                    await self._send_subscribe(ws)
                    resub_task = asyncio.create_task(self._resubscribe.wait())
                    recv_task = asyncio.create_task(ws.recv())
                    try:
                        while True:
                            done, _ = await asyncio.wait(
                                [resub_task, recv_task], return_when=asyncio.FIRST_COMPLETED
                            )
                            if recv_task in done:
                                try:
                                    message = recv_task.result()
                                except ConnectionClosed:
                                    break
                                except Exception as exc:
                                    logger.warning("user ws recv error: %s", exc)
                                    break
                                recv_task = asyncio.create_task(ws.recv())
                                await self._handle_message(message, on_order_event, on_trade_event)
                            if resub_task in done:
                                self._resubscribe.clear()
                                await self._send_subscribe(ws)
                                resub_task = asyncio.create_task(self._resubscribe.wait())
                    finally:
                        for task in (resub_task, recv_task):
                            if task and not task.done():
                                task.cancel()
            except Exception as exc:
                logger.warning("user ws error: %s", exc)
                await asyncio.sleep(1)

    async def _send_subscribe(self, ws: websockets.WebSocketClientProtocol) -> None:
        if not self._subscriptions:
            return
        payload = {
            "type": "user",
            "markets": list(self._subscriptions),
            "auth": {
                "apikey": self.api_key,
                "secret": self.api_secret,
                "passphrase": self.api_passphrase,
            },
        }
        await ws.send(json.dumps(payload))

    async def _handle_message(
        self,
        message: str,
        on_order_event: Callable[[dict], None],
        on_trade_event: Callable[[dict], None],
    ) -> None:
        if isinstance(message, bytes):
            message = message.decode("utf-8", "ignore")
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            logger.debug("user ws ignored non-json message")
            return
        if isinstance(payload, list):
            for item in payload:
                self._dispatch_payload(item, on_order_event, on_trade_event)
            return
        self._dispatch_payload(payload, on_order_event, on_trade_event)

    def _dispatch_payload(
        self,
        payload: object,
        on_order_event: Callable[[dict], None],
        on_trade_event: Callable[[dict], None],
    ) -> None:
        if not isinstance(payload, dict):
            return
        event_type = payload.get("type") or payload.get("event_type")
        if event_type == "order":
            on_order_event(payload)
        elif event_type == "trade":
            on_trade_event(payload)
        else:
            return
