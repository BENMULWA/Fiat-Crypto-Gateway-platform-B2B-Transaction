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

`credit_available`/`spend_locked` also append to `institutional_ledger_entries`
-- the one shared ledger backing the portal's Collections ("in"), Payouts
("out"), and Transactions History (both) pages, instead of three divergent
backend concepts. `lock_funds`/`release_locked` don't write an entry: neither
actually moves money in/out, only reserves/unreserves it.
"""
import uuid
from datetime import datetime

from fastapi import HTTPException


def _asset_key(asset: str) -> str:
    return str(asset or "").upper()


async def _write_ledger_entry(
    db, user_id: str, *, direction: str, asset: str, amount: float,
    source: str, related_rfq_id: str | None = None, related_settlement_id: str | None = None,
) -> None:
    await db["institutional_ledger_entries"].insert_one({
        "id": f"LEDGER-{uuid.uuid4().hex[:10].upper()}",
        "merchantId": str(user_id),
        "direction": direction,
        "asset": _asset_key(asset),
        "amount": float(amount),
        "source": source,
        "relatedRfqId": related_rfq_id,
        "relatedSettlementId": related_settlement_id,
        "status": "completed",
        "createdAt": datetime.utcnow(),
    })


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


async def credit_available(
    db, user_id: str, asset: str, amount: float,
    *, source: str = "credit", related_rfq_id: str | None = None, related_settlement_id: str | None = None,
) -> None:
    """
    Credits a confirmed deposit onto `available`. v1 deposit confirmation is
    manual (treasury confirms an incoming bank/crypto transfer the same way
    confirm_customer_funds does for dealer settlements) -- this is the
    function that call fires from, not an automated deposit watcher.
    """
    amount = float(amount or 0)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Credit amount must be positive")
    asset_key = _asset_key(asset)
    await db["institutional_wallets"].update_one(
        {"_id": user_id},
        {
            "$inc": {f"{asset_key}.available": amount},
            "$set": {"updatedAt": datetime.utcnow()},
            "$setOnInsert": {"_id": user_id},
        },
        upsert=True,
    )
    await _write_ledger_entry(
        db, user_id, direction="in", asset=asset_key, amount=amount,
        source=source, related_rfq_id=related_rfq_id, related_settlement_id=related_settlement_id,
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


async def log_settlement_ledger_entry(
    db, user_id: str, *, direction: str, asset: str, amount: float,
    source: str, related_rfq_id: str | None = None, related_settlement_id: str | None = None,
) -> None:
    """
    Records a ledger entry for a per-trade, quote-then-fund settlement leg
    WITHOUT touching institutional_wallets.available/locked -- unlike
    credit_available/spend_locked, this money was never held as a standing
    balance (it was sent to fund one specific accepted quote and used
    immediately), so there is no available/locked figure to move. Used by
    the settlement action state machine's confirm_customer_funds (direction
    "in") and reconciled (direction "out") transitions for merchant
    self-service RFQs -- see routes/otc_admin.py::act_on_dealer_settlement.
    """
    await _write_ledger_entry(
        db, user_id, direction=direction, asset=asset, amount=amount,
        source=source, related_rfq_id=related_rfq_id, related_settlement_id=related_settlement_id,
    )


async def spend_locked(
    db, user_id: str, asset: str, amount: float,
    *, related_rfq_id: str | None = None, related_settlement_id: str | None = None,
) -> None:
    """Settlement reconciled -- locked funds are actually gone, for real."""
    amount = float(amount or 0)
    if amount <= 0:
        return
    asset_key = _asset_key(asset)
    await db["institutional_wallets"].update_one(
        {"_id": user_id},
        {"$inc": {f"{asset_key}.locked": -amount}, "$set": {"updatedAt": datetime.utcnow()}},
    )
    await _write_ledger_entry(
        db, user_id, direction="out", asset=asset_key, amount=amount,
        source="settlement_spend", related_rfq_id=related_rfq_id, related_settlement_id=related_settlement_id,
    )
