"""Per-user Stellar deposit sub-accounts — the Stellar counterpart to
celo_wallet.py and cardano_child_wallet.py.

Derives a unique Stellar keypair per user from STELLAR_MNEMONIC via
stellar_sdk's standard SEP-5 mnemonic derivation. Unlike Celo/Cardano, a
Stellar keypair isn't a usable account the moment it's derived — Stellar
requires an account to be created on-chain (funded with a minimum XLM
reserve) before it can receive anything at all, and each non-XLM asset needs
its own funded trustline before that account can hold it. See
`provision_stellar_account` for that one-time on-chain setup step.

The treasury for Stellar is a freshly generated account (STELLAR_TREASURY_SECRET/
STELLAR_TREASURY_ADDRESS) — the previously configured STELLAR_MASTER_ADDRESS had
no known private key available to this backend and could not be signed from,
so it's abandoned rather than reused.
"""
from __future__ import annotations

import os
from datetime import datetime

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from stellar_sdk import Keypair


def derive_stellar_child_account(index: int) -> Keypair:
    """Return the Keypair for the given SEP-5 mnemonic index."""
    mnemonic = os.getenv("STELLAR_MNEMONIC")
    if not mnemonic:
        raise ValueError("STELLAR_MNEMONIC is not configured.")
    return Keypair.from_mnemonic_phrase(mnemonic, index=index)


def get_stellar_treasury_keypair() -> Keypair:
    secret = os.getenv("STELLAR_TREASURY_SECRET")
    if not secret:
        raise ValueError("STELLAR_TREASURY_SECRET is not configured.")
    return Keypair.from_secret(secret)


async def get_or_create_stellar_wallet_index(db, user_id) -> int:
    """Return this user's persistent Stellar deposit HD index, allocating one
    if needed. Own counter/collection, same pattern as celo_wallet.py and
    cardano_child_wallet.py."""
    user_id = str(user_id)

    existing = await db["stellar_child_wallet_indexes"].find_one({"_id": user_id})
    if existing:
        return existing["index"]

    counter = await db["stellar_child_wallet_counters"].find_one_and_update(
        {"_id": "global"},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    new_index = counter["value"]

    try:
        await db["stellar_child_wallet_indexes"].insert_one({
            "_id": user_id,
            "userId": user_id,
            "index": new_index,
            "createdAt": datetime.utcnow(),
        })
        return new_index
    except DuplicateKeyError:
        existing = await db["stellar_child_wallet_indexes"].find_one({"_id": user_id})
        return existing["index"]
