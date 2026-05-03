from __future__ import annotations

from decimal import Decimal
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, OrderArgs, ApiCreds

try:
    from py_clob_client.clob_types import OrderType, PostOrdersArgs, OpenOrderParams
except Exception:  # pragma: no cover - fallback if SDK changes
    OrderType = Any  # type: ignore
    PostOrdersArgs = Any  # type: ignore
    OpenOrderParams = Any  # type: ignore

from app.config import CLOB_HOST, Settings


class ClobClientWrapper:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._api_creds: dict[str, str] | None = None
        self.client = ClobClient(
            CLOB_HOST,
            key=settings.private_key,
            chain_id=settings.chain_id,
            signature_type=settings.signature_type,
            funder=settings.funder_address,
        )

    def init_api_creds(self) -> None:
        if self.settings.api_key and self.settings.api_secret and self.settings.api_passphrase:
            creds = ApiCreds(
                api_key=self.settings.api_key,
                api_secret=self.settings.api_secret,
                api_passphrase=self.settings.api_passphrase,
            )
            self.client.set_api_creds(creds)
            self._api_creds = self._normalize_creds(creds)
            return
        creds = self.client.create_or_derive_api_creds()
        self.client.set_api_creds(creds)
        self._api_creds = self._normalize_creds(creds)
        if not self._api_creds:
            self._api_creds = self._normalize_creds(getattr(self.client, "api_creds", None))

    def get_api_creds(self) -> dict[str, str] | None:
        if self._api_creds and all(self._api_creds.values()):
            return self._api_creds
        legacy = self._normalize_creds(getattr(self.client, "api_creds", None))
        if legacy and all(legacy.values()):
            return legacy
        return None

    @staticmethod
    def _normalize_creds(raw: object) -> dict[str, str] | None:
        if raw is None:
            return None
        if isinstance(raw, (list, tuple)) and len(raw) >= 3:
            key, secret, passphrase = raw[0], raw[1], raw[2]
            return {
                "apiKey": str(key),
                "apiSecret": str(secret),
                "apiPassphrase": str(passphrase),
            }
        if isinstance(raw, dict):
            key = raw.get("apiKey") or raw.get("api_key") or raw.get("apikey") or raw.get("key")
            secret = (
                raw.get("apiSecret")
                or raw.get("api_secret")
                or raw.get("apisecret")
                or raw.get("secret")
            )
            passphrase = (
                raw.get("apiPassphrase")
                or raw.get("api_passphrase")
                or raw.get("apipassphrase")
                or raw.get("passphrase")
            )
            if key and secret and passphrase:
                return {
                    "apiKey": str(key),
                    "apiSecret": str(secret),
                    "apiPassphrase": str(passphrase),
                }
            return None
        # Attribute-based fallback
        key = getattr(raw, "apiKey", None) or getattr(raw, "api_key", None) or getattr(raw, "key", None)
        secret = (
            getattr(raw, "apiSecret", None)
            or getattr(raw, "api_secret", None)
            or getattr(raw, "secret", None)
        )
        passphrase = (
            getattr(raw, "apiPassphrase", None)
            or getattr(raw, "api_passphrase", None)
            or getattr(raw, "passphrase", None)
        )
        if key and secret and passphrase:
            return {
                "apiKey": str(key),
                "apiSecret": str(secret),
                "apiPassphrase": str(passphrase),
            }
        return None

    def check_allowances(self) -> None:
        if not hasattr(self.client, "get_balance_allowance"):
            return
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        result = self.client.get_balance_allowance(params)
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(f"Allowance check failed: {result}")

    def get_order_book(self, token_id: str) -> Any:
        return self.client.get_order_book(token_id)

    def create_limit_order(
        self,
        token_id: str,
        side: str,
        price: Decimal,
        size: Decimal,
        tif_seconds: int | None = None,
    ) -> Any:
        expiration = 0
        if tif_seconds is not None:
            expiration = int(__import__("time").time()) + tif_seconds

        order_args = OrderArgs(
            token_id=token_id,
            price=float(price),
            size=float(size),
            side=side.upper(),
            expiration=expiration,
        )
        return self.client.create_order(order_args)

    def post_order(self, order: Any, order_type: str) -> Any:
        ot = getattr(OrderType, order_type, order_type)
        return self.client.post_order(order, ot)

    def post_orders(self, orders: list[Any], order_type: str = "FOK") -> Any:
        ot = getattr(OrderType, order_type, order_type)
        args = []
        for order in orders:
            try:
                args.append(PostOrdersArgs(order=order, order_type=ot))
            except TypeError:
                args.append(PostOrdersArgs(order=order))
        return self.client.post_orders(args)

    def get_open_orders(self, condition_id: str) -> Any:
        params = OpenOrderParams(condition_id=condition_id)
        return self.client.get_orders(params)

    def cancel_order(self, order_id: str) -> Any:
        if hasattr(self.client, "cancel_order"):
            return self.client.cancel_order(order_id)
        if hasattr(self.client, "cancel"):
            return self.client.cancel(order_id)
        raise RuntimeError("cancel_order not supported by SDK")

    def get_positions(self) -> list[dict]:
        """Get all positions for the current user."""
        if hasattr(self.client, "get_positions"):
            return self.client.get_positions() or []
        return []

    def get_position(self, token_id: str) -> tuple[int, int]:
        """Get position for a specific token (size in raw units, avg_price)."""
        if hasattr(self.client, "get_position"):
            result = self.client.get_position(token_id)
            if isinstance(result, tuple):
                return result
            if isinstance(result, dict):
                return (result.get("size", 0), result.get("avgPrice", 0))
        return (0, 0)

    def merge_positions(self, amount: int, condition_id: str, neg_risk: bool = False) -> Any:
        """Merge YES and NO positions to free up capital.

        When holding both YES and NO tokens, they can be merged back to USDC.
        Amount is in raw units (multiply shares by 10^6).
        """
        if hasattr(self.client, "merge_positions"):
            return self.client.merge_positions(amount, condition_id, neg_risk)
        raise RuntimeError("merge_positions not supported by SDK")
