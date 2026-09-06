#!/usr/bin/env python3
"""
Migration: Backfill all existing retail wallets with missing asset fields.

This script ensures all retail wallet documents have zero-initialized fields for all
supported assets, preventing the "no balance" display issue in the withdrawal UI.

Usage:
    python -m migrations.backfill_wallet_assets
"""

import asyncio
import sys
from pathlib import Path

# Add backend root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from database import get_db, get_client
from config import settings

# All supported assets in the platform (must match retail.py SUPPORTED_ASSETS)
SUPPORTED_ASSETS = [
    "KES", "USDA", "USDT", "USDC", "USD", 
    "UGX", "TZS", "RWF", "BIF", "XAF", "XOF", 
    "AIRT", "IMP", "BTC", "ETH"
]


async def backfill_wallets():
    """Backfill all existing wallets with missing asset fields."""
    db = get_db()
    
    print("🔍 Starting wallet asset backfill migration...")
    print(f"   Target assets: {', '.join(SUPPORTED_ASSETS)}")
    
    try:
        # Get all retail wallets
        wallets = await db["retail_wallets"].find({}).to_list(length=None)
        print(f"📊 Found {len(wallets)} wallets to check")
        
        if not wallets:
            print("✅ No wallets to migrate")
            return
        
        updated_count = 0
        
        for wallet in wallets:
            wallet_id = wallet.get("_id")
            user_id = wallet.get("userId")
            
            # Build update dict with all assets, preserving existing values
            update_fields = {}
            has_missing = False
            
            for asset in SUPPORTED_ASSETS:
                if asset not in wallet:
                    update_fields[asset] = 0.0
                    has_missing = True
            
            # Only update if there are missing fields
            if has_missing:
                result = await db["retail_wallets"].update_one(
                    {"_id": wallet_id},
                    {"$set": update_fields}
                )
                
                if result.modified_count > 0:
                    updated_count += 1
                    missing_assets = ", ".join(update_fields.keys())
                    print(f"  ✓ {user_id}: added {missing_assets}")
        
        print(f"\n✅ Migration complete!")
        print(f"   Updated: {updated_count} wallets")
        print(f"   Skipped: {len(wallets) - updated_count} wallets (already complete)")
        
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        raise
    finally:
        # Close database connection
        client = get_client()
        if client:
            client.close()


if __name__ == "__main__":
    print("\n" + "="*60)
    print("RETAIL WALLET ASSET BACKFILL MIGRATION")
    print("="*60 + "\n")
    
    asyncio.run(backfill_wallets())
    
    print("\n" + "="*60)
    print("Migration finished. You can now restart the server.")
    print("="*60 + "\n")
