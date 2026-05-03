from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase


async def connect_mongo(uri: str, db_name: str) -> AsyncIOMotorDatabase:
    client = AsyncIOMotorClient(uri)
    return client[db_name]


async def ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    await db.markets.create_index("created_at")
    await db.markets.create_index("status")

    await db.cycles.create_index("cycle_id", unique=True)
    await db.cycles.create_index([("condition_id", 1), ("timestamps.created_at", 1)])
    await db.cycles.create_index("state")

    await db.orders.create_index("order_id", unique=True)
    await db.orders.create_index("cycle_id")
    await db.orders.create_index("condition_id")

    await db.trades.create_index("trade_id", unique=True)
    await db.trades.create_index("taker_order_id")
    await db.trades.create_index("cycle_id")
    await db.trades.create_index("status")

    await db.ws_events.create_index("ts", expireAfterSeconds=604800)
