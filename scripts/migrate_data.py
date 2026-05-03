#!/usr/bin/env python3
"""
Data Migration Script

Migrates data from old unified tables to new partitioned tables:
- market_rounds -> market_rounds_crypto_15min
- snapshots -> snapshots_{asset}_15min

Usage:
    python scripts/migrate_data.py
"""
import asyncio
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv


async def migrate():
    load_dotenv()

    uri = os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017")
    db_name = os.getenv("MONGODB_DB", "polymarket_maker")

    client = AsyncIOMotorClient(uri)
    db = client[db_name]

    print("=" * 60)
    print("Data Migration: Old -> New Table Structure")
    print("=" * 60)

    # Check if old tables exist
    collections = await db.list_collection_names()

    # ==================== Migrate market_rounds ====================
    if "market_rounds" in collections:
        old_count = await db.market_rounds.count_documents({})
        print(f"\nFound old 'market_rounds' table: {old_count} documents")

        if old_count > 0:
            # Group by asset and migrate
            pipeline = [{"$group": {"_id": "$asset", "count": {"$sum": 1}}}]
            async for group in db.market_rounds.aggregate(pipeline):
                asset = group["_id"]
                count = group["count"]
                print(f"  - {asset}: {count} documents")

            # Migrate to new table
            new_coll = "market_rounds_crypto_15min"
            print(f"\nMigrating to '{new_coll}'...")

            # Copy all documents
            async for doc in db.market_rounds.find():
                # Add new fields if missing
                if "market_type" not in doc:
                    doc["market_type"] = "crypto"
                if "duration" not in doc:
                    doc["duration"] = "15min"
                if "duration_sec" not in doc:
                    doc["duration_sec"] = 900

                # Upsert to avoid duplicates
                await db[new_coll].update_one(
                    {"condition_id": doc["condition_id"]},
                    {"$set": doc},
                    upsert=True
                )

            new_count = await db[new_coll].count_documents({})
            print(f"  Migrated {new_count} documents to '{new_coll}'")

            # Create indexes
            await db[new_coll].create_index("condition_id", unique=True)
            await db[new_coll].create_index("asset")
            await db[new_coll].create_index("round_ts")
            await db[new_coll].create_index([("asset", 1), ("round_ts", -1)])
            print(f"  Created indexes for '{new_coll}'")
    else:
        print("\nNo old 'market_rounds' table found")

    # ==================== Migrate snapshots ====================
    if "snapshots" in collections:
        old_count = await db.snapshots.count_documents({})
        print(f"\nFound old 'snapshots' table: {old_count} documents")

        if old_count > 0:
            # We need to determine asset from market_id
            # First, build market_id -> asset mapping
            market_asset_map = {}
            async for doc in db.market_rounds.find({}, {"_id": 1, "asset": 1}):
                market_asset_map[doc["_id"]] = doc.get("asset", "BTC")

            # Also check new table
            if "market_rounds_crypto_15min" in collections:
                async for doc in db.market_rounds_crypto_15min.find({}, {"_id": 1, "asset": 1}):
                    market_asset_map[doc["_id"]] = doc.get("asset", "BTC")

            # Count by asset
            asset_counts = {}
            async for doc in db.snapshots.find({}, {"market_id": 1}):
                market_id = doc.get("market_id")
                asset = market_asset_map.get(market_id, "UNKNOWN")
                asset_counts[asset] = asset_counts.get(asset, 0) + 1

            for asset, count in sorted(asset_counts.items()):
                print(f"  - {asset}: {count} documents")

            # Migrate each asset to separate table
            for asset in asset_counts.keys():
                if asset == "UNKNOWN":
                    continue

                new_coll = f"snapshots_{asset.lower()}_15min"
                print(f"\nMigrating {asset} snapshots to '{new_coll}'...")

                # Get market_ids for this asset
                asset_market_ids = [
                    mid for mid, a in market_asset_map.items() if a == asset
                ]

                migrated = 0
                async for doc in db.snapshots.find({"market_id": {"$in": asset_market_ids}}):
                    # Add asset field if missing
                    if "asset" not in doc:
                        doc["asset"] = asset

                    # Extract round_ts from market if not present
                    if "round_ts" not in doc:
                        # Try to get from market_rounds
                        market_doc = await db.market_rounds.find_one({"_id": doc["market_id"]})
                        if market_doc:
                            doc["round_ts"] = market_doc.get("round_ts", 0)

                    # Insert (not upsert, snapshots should be unique by time)
                    try:
                        await db[new_coll].insert_one(doc)
                        migrated += 1
                    except Exception:
                        pass  # Duplicate

                print(f"  Migrated {migrated} documents to '{new_coll}'")

                # Create indexes
                await db[new_coll].create_index("market_id")
                await db[new_coll].create_index("ts_utc")
                await db[new_coll].create_index("round_ts")
                await db[new_coll].create_index([("market_id", 1), ("ts_utc", 1)])
                await db[new_coll].create_index([("round_ts", 1), ("ts_utc", 1)])
                print(f"  Created indexes for '{new_coll}'")
    else:
        print("\nNo old 'snapshots' table found")

    # ==================== Summary ====================
    print("\n" + "=" * 60)
    print("Migration Complete - New Table Structure")
    print("=" * 60)

    new_collections = await db.list_collection_names()
    for coll in sorted(new_collections):
        if coll.startswith("market_rounds_") or coll.startswith("snapshots_"):
            count = await db[coll].count_documents({})
            print(f"  {coll}: {count} documents")

    print("\n" + "=" * 60)
    print("Old tables preserved (can be dropped manually if migration successful)")
    print("  - market_rounds")
    print("  - snapshots")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(migrate())
