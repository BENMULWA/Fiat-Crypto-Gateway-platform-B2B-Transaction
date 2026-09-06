"""Shared wallet balance helpers that account for the recurring string/ObjectId
userId split in retail_wallets — the same user can have funds spread across a
legacy string-keyed row and a newer ObjectId-keyed row (see the "user-id type
mismatch" pattern that recurs across retail.py/valora.py/auth.py).

GET /api/retail/wallet already sums across every matching row when displaying
a balance. Every withdrawal endpoint's balance check was instead doing a
plain find_one — looking at only one row — so a user could see "1.5177 USDC"
on screen and still get rejected with "you have 0.0177" at withdrawal time.
These helpers make the check and the debit agree with what's displayed.
"""
from fastapi import HTTPException

from routes.retail import build_user_id_candidates


async def get_summed_balance(db, user_id_raw, asset: str) -> float:
    """Total balance for `asset` across every retail_wallets row matching any
    representation of this user's id — the same computation GET /wallet uses."""
    candidates = build_user_id_candidates(user_id_raw)
    wallets = await db["retail_wallets"].find({"userId": {"$in": candidates}}).to_list(length=100)
    return sum(float(w.get(asset, 0.0) or 0.0) for w in wallets)


async def debit_wallet(db, user_id_raw, asset: str, amount: float) -> None:
    """Debit `amount` of `asset`, atomically, even when balance is split
    across multiple rows. Raises HTTPException(400) if the combined balance
    is insufficient. Must be called before any provider/on-chain call — the
    debit happening first is what lets a failed send be safely refunded.
    """
    candidates = build_user_id_candidates(user_id_raw)
    wallets = await db["retail_wallets"].find({"userId": {"$in": candidates}}).to_list(length=100)

    total = sum(float(w.get(asset, 0.0) or 0.0) for w in wallets)
    if total < amount:
        raise HTTPException(status_code=400, detail=f"Insufficient {asset} balance. You have {round(total, 6)}.")

    # Common case: a single row alone covers the amount — one atomic,
    # $gte-guarded update, same pattern the rest of the codebase already uses.
    for w in wallets:
        bal = float(w.get(asset, 0.0) or 0.0)
        if bal >= amount:
            result = await db["retail_wallets"].update_one(
                {"_id": w["_id"], asset: {"$gte": amount}},
                {"$inc": {asset: -amount}},
            )
            if result.modified_count == 1:
                return
            # Another request drained this exact row between our read and
            # this write — fall through to the split-row path below, which
            # re-reads fresh balances rather than trusting the stale list.
            break

    # No single row covers it (or the attempt above lost a race): debit
    # sequentially across rows, rolling back anything already taken if the
    # combined total turns out to not actually be reachable (a concurrent
    # withdrawal beat us to some of it).
    remaining = amount
    debited: list[tuple] = []
    fresh_wallets = await db["retail_wallets"].find({"userId": {"$in": candidates}}).to_list(length=100)
    for w in fresh_wallets:
        if remaining <= 1e-9:
            break
        bal = float(w.get(asset, 0.0) or 0.0)
        if bal <= 0:
            continue
        take = min(bal, remaining)
        result = await db["retail_wallets"].update_one(
            {"_id": w["_id"], asset: {"$gte": take}},
            {"$inc": {asset: -take}},
        )
        if result.modified_count == 1:
            debited.append((w["_id"], take))
            remaining -= take

    if remaining > 1e-9:
        for wallet_id, take in debited:
            await db["retail_wallets"].update_one({"_id": wallet_id}, {"$inc": {asset: take}})
        raise HTTPException(status_code=400, detail=f"Insufficient {asset} balance. Please try again.")


async def credit_wallet(db, user_id_raw, asset: str, amount: float) -> None:
    """Refund/credit `amount` back onto whichever row already represents this
    user, to undo a debit_wallet() call after a downstream failure. Prefers
    an existing row over creating a new one, matching how debit reads rows."""
    candidates = build_user_id_candidates(user_id_raw)
    result = await db["retail_wallets"].update_one(
        {"userId": {"$in": candidates}},
        {"$inc": {asset: amount}},
    )
    if result.matched_count == 0:
        # No row exists for any candidate id at all (shouldn't normally
        # happen — debit_wallet only succeeds if a row existed to debit from).
        await db["retail_wallets"].update_one(
            {"userId": user_id_raw},
            {"$inc": {asset: amount}},
            upsert=True,
        )
