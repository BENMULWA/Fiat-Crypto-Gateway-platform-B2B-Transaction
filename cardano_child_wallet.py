"""Per-user Cardano deposit sub-accounts — the Cardano counterpart to
celo_wallet.py.

Derives a unique Cardano payment address per user from CARDANO_MNEMONIC via
standard CIP-1852 HD derivation (m/1852'/1815'/{index}'/0/0). This is
deliberately separate from `cardano-USDA/wallet.py`'s `CardanoWallet`, which
is the platform's single custodial master wallet (used for treasury-side
settlement/minting) — that class explicitly ignores its `account_index`
argument today ("Everyone uses the exact same Master Wallet"), so it cannot
be reused for per-user isolation without changing its behavior for every
existing caller. This module only adds NEW per-user child addresses; the
master wallet keeps working exactly as it does today for treasury operations.

The private key for a given index is never stored — it's re-derived on
demand from CARDANO_MNEMONIC + index whenever it's needed to sign a sweep
transaction, the same custody model used for Celo child wallets.
"""
from __future__ import annotations

import os
from datetime import datetime

from pycardano import Address, HDWallet, PaymentExtendedSigningKey
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from cardano.client import get_network


# Cardano Shelley-era ledger specification (CIP-1852) 
CARDANO_DERIVATION_PATH = "m/1852'/1815'/{index}'/0/0"


def derive_cardano_child_account(index: int):
    """Return (signing_key, address) for the given HD index."""
    mnemonic = os.getenv("CARDANO_MNEMONIC")
    if not mnemonic:
        raise ValueError("CARDANO_MNEMONIC is not configured.")

    hdwallet = HDWallet.from_mnemonic(mnemonic)
    child = hdwallet.derive_from_path(CARDANO_DERIVATION_PATH.format(index=index))
    signing_key = PaymentExtendedSigningKey.from_hdwallet(child)
    verification_key = signing_key.to_verification_key()
    address = Address(payment_part=verification_key.hash(), network=get_network())
    return signing_key, address


async def get_or_create_cardano_wallet_index(db, user_id) -> int:
    """Return this user's persistent Cardano deposit HD index, allocating one
    if needed. Uses its own counter/collection — kept separate from the
    legacy workspace-keyed `cardano_wallets` collection used by the shared
    custodial master wallet, to avoid mixing the two index spaces."""
    user_id = str(user_id)

    existing = await db["cardano_child_wallet_indexes"].find_one({"_id": user_id})
    if existing:
        return existing["index"]

    counter = await db["cardano_child_wallet_counters"].find_one_and_update(
        {"_id": "global"},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    new_index = counter["value"]

    try:
        await db["cardano_child_wallet_indexes"].insert_one({
            "_id": user_id,
            "userId": user_id,
            "index": new_index,
            "createdAt": datetime.utcnow(),
        })
        return new_index
    except DuplicateKeyError:
        # Another request allocated this user's index concurrently — use theirs.
        existing = await db["cardano_child_wallet_indexes"].find_one({"_id": user_id})
        return existing["index"]
