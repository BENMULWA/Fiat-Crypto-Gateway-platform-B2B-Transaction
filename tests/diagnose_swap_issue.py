#!/usr/bin/env python3
"""
Diagnose why swaps show as COMPLETED but funds aren't in wallets.
Inspects swap records and wallet state.
"""
import asyncio
import sys
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
import os

load_dotenv(override=True)

async def diagnose_swaps():
    # Connect to MongoDB - use credentials from .env
    mongo_uri = os.getenv("MONGO_URL", "mongodb://localhost:27017")
    mongo_db = os.getenv("MONGO_DB", "Crypto_Payment_Gateway")
    
    client = AsyncIOMotorClient(mongo_uri)
    db = client[mongo_db]
    
    print("=" * 80)
    print("SWAP ISSUE DIAGNOSIS")
    print("=" * 80)
    print(f"Connected to: {mongo_uri}")
    print(f"Database: {mongo_db}")
    print()
    
    # Get all recent swaps (last 24 hours to be safe)
    cutoff = datetime.utcnow() - timedelta(hours=24)
    recent_swaps = await db["ramp_entries"].find({
        "createdAt": {"$gte": cutoff},
        "direction": "swap"
    }).sort("_id", -1).to_list(length=50)
    
    print(f"Recent swaps in last 24 hours: {len(recent_swaps)}")
    print("-" * 80)
    
    # If none found, check if ANY swaps exist
    if len(recent_swaps) == 0:
        print("No swaps found in last 24 hours. Checking if ANY swaps exist...")
        all_swaps_count = await db["ramp_entries"].count_documents({"direction": "swap"})
        print(f"Total swaps in database: {all_swaps_count}")
        if all_swaps_count > 0:
            # Get the 5 most recent
            recent_swaps = await db["ramp_entries"].find({"direction": "swap"}).sort("_id", -1).to_list(length=5)
            print(f"Showing {len(recent_swaps)} most recent swaps (older than 24 hours):\n")
    
    for swap in recent_swaps:
        swap_id = swap.get("_id")
        user_id = swap.get("userId")
        status = swap.get("status")
        success = swap.get("success")
        from_asset = swap.get("fromAsset")
        to_asset = swap.get("toAsset")
        from_amount = swap.get("fromAmount")
        to_amount = swap.get("toAmount")
        error = swap.get("error")
        tx_hash = swap.get("txHash")
        settled_at = swap.get("settledAt")
        created_at = swap.get("createdAt")
        
        print(f"\nSwap ID: {swap_id}")
        print(f"  User: {user_id}")
        print(f"  Status: {status} | Success: {success}")
        print(f"  Trade: {from_amount} {from_asset} → {to_amount} {to_asset}")
        print(f"  Created: {created_at.isoformat() if created_at else 'N/A'}")
        print(f"  Settled: {settled_at.isoformat() if settled_at else 'N/A'}")
        if tx_hash:
            print(f"  TX/Ref: {tx_hash}")
        if error:
            print(f"  ❌ Error: {error}")
        
        # Check wallet state
        print(f"  📊 Wallet State:")
        wallet = await db["retail_wallets"].find_one({"userId": user_id})
        if not wallet:
            # Try ObjectId
            from bson import ObjectId
            try:
                wallet = await db["retail_wallets"].find_one({"userId": ObjectId(user_id)})
            except:
                pass
        
        if wallet:
            print(f"     {from_asset}: {wallet.get(from_asset, 0.0)}")
            print(f"     {to_asset}: {wallet.get(to_asset, 0.0)}")
            print(f"     (Expected {to_asset}: {wallet.get(to_asset, 0.0) + to_amount if success else 'N/A'})")
        else:
            print(f"     ❌ Wallet not found for user {user_id}")
    
    print("\n" + "=" * 80)
    print("KEY ISSUES TO CHECK:")
    print("1. Are swaps marked COMPLETED but have success=False?")
    print("2. Are wallets showing zero balance even after settlement?")
    print("3. Are there TX hashes/references? Check if on-chain/API confirmed.")
    print("4. Compare created vs settled timestamps - was there a delay?")
    print("=" * 80)
    
    client.close()

if __name__ == "__main__":
    asyncio.run(diagnose_swaps())
