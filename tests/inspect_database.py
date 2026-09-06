#!/usr/bin/env python3
"""
Inspect database structure and find where swap data might be stored.
"""
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
import os

load_dotenv(override=True)

async def inspect_database():
    mongo_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    client = AsyncIOMotorClient(mongo_uri)
    db = client["meshex"]
    
    print("=" * 80)
    print("DATABASE INSPECTION")
    print("=" * 80)
    print(f"Connected to: {mongo_uri}")
    print()
    
    # Get all collections
    collections = await db.list_collection_names()
    print(f"Collections in 'meshex' database ({len(collections)}):")
    for col in sorted(collections):
        count = await db[col].count_documents({})
        print(f"  - {col}: {count} documents")
    
    print("\n" + "=" * 80)
    print("RAMP_ENTRIES COLLECTION ANALYSIS")
    print("=" * 80)
    
    ramp_count = await db["ramp_entries"].count_documents({})
    print(f"Total ramp_entries: {ramp_count}")
    
    if ramp_count > 0:
        # Group by direction
        pipeline = [
            {"$group": {"_id": "$direction", "count": {"$sum": 1}}}
        ]
        results = await db["ramp_entries"].aggregate(pipeline).to_list(length=None)
        print("By direction:")
        for r in results:
            print(f"  {r['_id']}: {r['count']}")
        
        # Group by status
        pipeline = [
            {"$group": {"_id": "$status", "count": {"$sum": 1}}}
        ]
        results = await db["ramp_entries"].aggregate(pipeline).to_list(length=None)
        print("By status:")
        for r in results:
            print(f"  {r['_id']}: {r['count']}")
    
    print("\n" + "=" * 80)
    print("RETAIL_WALLETS ANALYSIS")
    print("=" * 80)
    
    wallet_count = await db["retail_wallets"].count_documents({})
    print(f"Total wallets: {wallet_count}")
    
    if wallet_count > 0:
        # Show sample wallet
        sample = await db["retail_wallets"].find_one({})
        print("\nSample wallet structure:")
        print(f"  userId: {sample.get('userId')} (type: {type(sample.get('userId')).__name__})")
        print(f"  Keys: {list(sample.keys())}")
        
        # Show wallet balances
        print("\nSample wallet balances:")
        for key in sample.keys():
            if key not in ['_id', 'userId']:
                print(f"  {key}: {sample[key]}")
    
    client.close()

if __name__ == "__main__":
    asyncio.run(inspect_database())
