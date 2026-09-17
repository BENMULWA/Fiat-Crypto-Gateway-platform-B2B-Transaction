"""
Institutional (OTC merchant) wallet ledger -- available/locked balances per
merchant per asset, in `institutional_wallets` (one document per user,
keyed by `_id` = user_id string).

Deliberately a simpler model than retail_wallets/wallet_utils.py: retail's
split-row-per-userId-representation complexity there exists to work around a
historical bug (funds spread across a legacy string-keyed row and a newer
ObjectId-keyed row), not because that's a good design to copy. Institutional
wallets are new, so there's no legacy split to account for -- one document
per merchant, one field per asset, each holding {available, locked}.

State machine mirrors dealer_engine/positions.py::TreasuryPositionEngine
exactly, just per-merchant instead of platform-wide:
  credit_available  -- deposit confirmed, funds usable
  lock_funds        -- RFQ accepted, funds committed but not yet spent
  release_locked    -- settlement failed/cancelled, funds freed back up
  spend_locked      -- settlement reconciled, funds actually gone
"""
from datetime import datetime

from fastapi import HTTPException


def _asset_key(asset: str) -> str:
    return str(asset or "").upper()


async def get_institutional_wallet(db, user_id: str) -> dict:
    wallet = await db["institutional_wallets"].find_one({"_id": user_id})
    return wallet or {"_id": user_id}


async def get_balance(db, user_id: str, asset: str) -> dict:
    """Returns {"available": float, "locked": float} for one asset."""
    wallet = await get_institutional_wallet(db, user_id)
    entry = wallet.get(_asset_key(asset)) or {}
    return {
        "available": float(entry.get("available", 0.0) or 0.0),
        "locked": float(entry.get("locked", 0.0) or 0.0),
    }


async def credit_available(db, user_id: str, asset: str, amount: float) -> None:
    """
    Credits a confirmed deposit onto `available`. v1 deposit confirmation is
    manual (treasury confirms an incoming bank/crypto transfer the same way
    confirm_customer_funds does for dealer settlements) -- this is the
    function that call fires from, not an automated deposit watcher.
    """
    amount = float(amount or 0)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Credit amount must be positive")
    asset = _asset_key(asset)
    await db["institutional_wallets"].update_one(
        {"_id": user_id},
        {
            "$inc": {f"{asset}.available": amount},
            "$set": {"updatedAt": datetime.utcnow()},
            "$setOnInsert": {"_id": user_id},
        },
        upsert=True,
    )


async def lock_funds(db, user_id: str, asset: str, amount: float) -> bool:
    """
    Moves `amount` from available to locked, atomically -- called when a
    merchant's RFQ is accepted. Returns False (does nothing) rather than
    raising if the available balance is insufficient, so callers can decide
    how to surface that (matches TreasuryPositionEngine.reserve's contract).
    """
    amount = float(amount or 0)
    if amount <= 0:
        return False
    asset = _asset_key(asset)
    result = await db["institutional_wallets"].update_one(
        {"_id": user_id, f"{asset}.available": {"$gte": amount}},
        {"$inc": {f"{asset}.available": -amount, f"{asset}.locked": amount}, "$set": {"updatedAt": datetime.utcnow()}},
    )
    return bool(result.modified_count)


async def release_locked(db, user_id: str, asset: str, amount: float) -> None:
    """Settlement failed/cancelled -- locked funds go back to available."""
    amount = float(amount or 0)
    if amount <= 0:
        return
    asset = _asset_key(asset)
    await db["institutional_wallets"].update_one(
        {"_id": user_id},
        {"$inc": {f"{asset}.locked": -amount, f"{asset}.available": amount}, "$set": {"updatedAt": datetime.utcnow()}},
    )


async def spend_locked(db, user_id: str, asset: str, amount: float) -> None:
    """Settlement reconciled -- locked funds are actually gone, for real."""
    amount = float(amount or 0)
    if amount <= 0:
        return
    asset = _asset_key(asset)
    await db["institutional_wallets"].update_one(
        {"_id": user_id},
        {"$inc": {f"{asset}.locked": -amount}, "$set": {"updatedAt": datetime.utcnow()}},
    )
