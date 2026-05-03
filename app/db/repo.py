from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.model.market import MarketMeta


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_dt(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class Repo:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self.db = db

    async def upsert_market(self, market: MarketMeta, discovered_via: str) -> None:
        await self.db.markets.update_one(
            {"_id": market.condition_id},
            {
                "$set": {
                    "condition_id": market.condition_id,
                    "slug": market.slug,
                    "question": market.question,
                    "created_at": market.created_at.isoformat(),
                    "end_at": _normalize_dt(market.end_at),
                    "outcomes": list(market.outcomes),
                    "token_ids": market.token_ids,
                    "tags": ["BTC", "15min"],
                    "status": market.status,
                    "updated_at": _now_iso(),
                    "order_min_size": market.order_min_size,
                },
                "$addToSet": {"discovered_via": discovered_via},
                "$setOnInsert": {"_id": market.condition_id},
            },
            upsert=True,
        )

    async def list_active_markets(self, limit: int = 10) -> list[dict[str, Any]]:
        cursor = (
            self.db.markets.find({"status": "ACTIVE"})
            .sort("created_at", -1)
            .limit(limit)
        )
        return [doc async for doc in cursor]

    async def create_cycle(self, condition_id: str, mode: str, params: dict) -> str:
        cycle_id = str(uuid.uuid4())
        await self.db.cycles.insert_one(
            {
                "_id": cycle_id,
                "cycle_id": cycle_id,
                "condition_id": condition_id,
                "mode": mode,
                "params": params,
                "state": "CREATED",
                "legs": [],
                "fills": {},
                "pnl": {"expected_edge": params.get("expected_edge")},
                "timestamps": {"created_at": _now_iso(), "submitted_at": None, "closed_at": None},
            }
        )
        return cycle_id

    async def update_cycle_state(self, cycle_id: str, state: str, **kwargs: Any) -> None:
        update = {"$set": {"state": state, **kwargs}}
        await self.db.cycles.update_one({"_id": cycle_id}, update)

    async def attach_cycle_legs(self, cycle_id: str, legs: list[dict]) -> None:
        await self.db.cycles.update_one({"_id": cycle_id}, {"$set": {"legs": legs}})

    async def update_cycle_fills(self, cycle_id: str, fills: dict) -> None:
        await self.db.cycles.update_one({"_id": cycle_id}, {"$set": {"fills": fills}})

    async def close_cycle(self, cycle_id: str, state: str, realized_edge: float | None = None) -> None:
        payload: dict[str, Any] = {
            "state": state,
            "timestamps.closed_at": _now_iso(),
        }
        if realized_edge is not None:
            payload["pnl.realized_edge"] = realized_edge
        await self.db.cycles.update_one({"_id": cycle_id}, {"$set": payload})

    async def upsert_order(self, order_id: str, payload: dict) -> None:
        payload["updated_at"] = _now_iso()
        await self.db.orders.update_one(
            {"_id": order_id},
            {"$set": payload, "$setOnInsert": {"_id": order_id}},
            upsert=True,
        )

    async def upsert_trade(self, trade_id: str, payload: dict) -> None:
        payload["updated_at"] = _now_iso()
        await self.db.trades.update_one(
            {"_id": trade_id},
            {"$set": payload, "$setOnInsert": {"_id": trade_id}},
            upsert=True,
        )

    async def log_audit(self, event: str, payload: dict) -> None:
        await self.db.audit_logs.insert_one({"ts": _now_dt(), "event": event, **payload})

    async def log_ws_event(
        self,
        channel: str,
        event_type: str,
        payload: dict,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> None:
        await self.db.ws_events.insert_one(
            {
                "ts": _now_dt(),
                "channel": channel,
                "event_type": event_type,
                "condition_id": condition_id,
                "token_id": token_id,
                "payload": payload,
            }
        )

    async def close_stale_submitted_cycles(self, max_age_sec: float = 60.0) -> int:
        """Close SUBMITTED/CREATED cycles that are too old (FOK orders that didn't fill).

        Returns number of cycles closed.
        """
        from datetime import timedelta

        cutoff = (_now_dt() - timedelta(seconds=max_age_sec)).isoformat()
        result = await self.db.cycles.update_many(
            {
                "state": {"$in": ["CREATED", "SUBMITTED"]},
                "timestamps.closed_at": None,
                "timestamps.created_at": {"$lt": cutoff},
            },
            {
                "$set": {
                    "state": "EXPIRED",
                    "timestamps.closed_at": _now_iso(),
                }
            },
        )
        return result.modified_count

    async def find_open_cycles(self) -> list[dict[str, Any]]:
        cursor = self.db.cycles.find(
            {
                "state": {"$in": ["CREATED", "SUBMITTED", "PARTIAL", "HEDGED"]},
                "timestamps.closed_at": None,
            }
        )
        return [doc async for doc in cursor]

    async def get_recent_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get most recent trades for dashboard display."""
        cursor = self.db.trades.find().sort("updated_at", -1).limit(limit)
        return [doc async for doc in cursor]

    async def get_position_summary(self) -> dict[str, Any]:
        """Aggregate position data from trades collection.

        Returns summary of UP and DOWN positions with qty, avg_price, cost, pnl.
        """
        pipeline = [
            {
                "$group": {
                    "_id": "$side",
                    "total_qty": {"$sum": {"$toDouble": {"$ifNull": ["$size", "0"]}}},
                    "total_cost": {"$sum": {"$multiply": [
                        {"$toDouble": {"$ifNull": ["$price", "0"]}},
                        {"$toDouble": {"$ifNull": ["$size", "0"]}}
                    ]}},
                    "count": {"$sum": 1}
                }
            }
        ]
        results = {}
        async for doc in self.db.trades.aggregate(pipeline):
            side = doc["_id"]
            qty = doc["total_qty"]
            cost = doc["total_cost"]
            avg_price = cost / qty if qty > 0 else 0
            results[side] = {
                "qty": qty,
                "avg_price": avg_price,
                "cost": cost,
                "count": doc["count"]
            }
        return results

    async def get_dashboard_stats(self) -> dict[str, Any]:
        """Get aggregated stats for dashboard."""
        trades_count = await self.db.trades.count_documents({})
        orders_count = await self.db.orders.count_documents({})
        cycles_count = await self.db.cycles.count_documents({})
        active_cycles = await self.db.cycles.count_documents({
            "state": {"$in": ["CREATED", "SUBMITTED", "PARTIAL", "HEDGED"]}
        })

        # Calculate total volume from trades
        volume_pipeline = [
            {
                "$group": {
                    "_id": None,
                    "total_volume": {"$sum": {"$multiply": [
                        {"$toDouble": {"$ifNull": ["$price", "0"]}},
                        {"$toDouble": {"$ifNull": ["$size", "0"]}}
                    ]}}
                }
            }
        ]
        volume = 0
        async for doc in self.db.trades.aggregate(volume_pipeline):
            volume = doc.get("total_volume", 0)

        return {
            "trades_count": trades_count,
            "orders_count": orders_count,
            "cycles_count": cycles_count,
            "active_cycles": active_cycles,
            "total_volume": volume
        }

    async def get_recent_cycles(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get most recent cycles for dashboard display."""
        cursor = self.db.cycles.find().sort("timestamps.created_at", -1).limit(limit)
        return [doc async for doc in cursor]

    async def get_cycle_stats(self) -> dict[str, Any]:
        """Get cycle statistics by state."""
        pipeline = [
            {
                "$group": {
                    "_id": "$state",
                    "count": {"$sum": 1},
                    "total_expected_edge": {"$sum": {"$toDouble": {"$ifNull": ["$pnl.expected_edge", "0"]}}}
                }
            }
        ]
        results = {}
        async for doc in self.db.cycles.aggregate(pipeline):
            results[doc["_id"]] = {
                "count": doc["count"],
                "total_expected_edge": doc["total_expected_edge"]
            }
        return results
