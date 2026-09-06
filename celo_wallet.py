"""Per-user Celo deposit sub-accounts.

Derives a unique Celo address per user from CELO_MNEMONIC via standard BIP-44
HD derivation (m/44'/60'/0'/0/{index}), the same way `cardano/wallet.py`
derives a unique Cardano address per user. This lets the deposit watcher
attribute an incoming ERC-20 transfer to the exact user who owns the address,
instead of guessing by matching amounts against a single shared treasury
address (see RETAIL_GO_LIVE_CHECKLIST.md).

The private key for a given index is never stored — it's re-derived on demand
from CELO_MNEMONIC + index whenever it's needed to sign a sweep transaction.
"""
import os
from datetime import datetime

from eth_account import Account
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

Account.enable_unaudited_hdwallet_features()

CELO_DERIVATION_PATH = "m/44'/60'/0'/0/{index}"


def derive_celo_account(index: int):
    """Return the eth_account LocalAccount for the given HD index."""
    mnemonic = os.getenv("CELO_MNEMONIC")
    if not mnemonic:
        raise ValueError("CELO_MNEMONIC is not configured.")
    return Account.from_mnemonic(mnemonic, account_path=CELO_DERIVATION_PATH.format(index=index))


async def get_or_create_celo_wallet_index(db, user_id) -> int:
    """Return this user's persistent Celo deposit HD index, allocating one if needed."""
    user_id = str(user_id)

    existing = await db["celo_wallet_indexes"].find_one({"_id": user_id})
    if existing:
        return existing["index"]

    counter = await db["celo_wallet_counters"].find_one_and_update(
        {"_id": "global"},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    new_index = counter["value"]

    try:
        await db["celo_wallet_indexes"].insert_one({
            "_id": user_id,
            "userId": user_id,
            "index": new_index,
            "createdAt": datetime.utcnow(),
        })
        return new_index
    except DuplicateKeyError:
        # Another request allocated this user's index concurrently — use theirs.
        existing = await db["celo_wallet_indexes"].find_one({"_id": user_id})
        return existing["index"]
