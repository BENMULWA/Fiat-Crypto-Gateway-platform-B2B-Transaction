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
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

Account.enable_unaudited_hdwallet_features()

CELO_DERIVATION_PATH = "m/44'/60'/0'/0/{index}"

_w3 = Web3(Web3.HTTPProvider(os.getenv("CELO_RPC_URL", "https://forno.celo.org"), request_kwargs={'timeout': 15}))
_w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

_ASSET_CONTRACTS = {
    "cUSD": _w3.to_checksum_address("0x765DE816845861e75A25fCA122bb6898B8B1282a"),
    "USDC": _w3.to_checksum_address("0xcebA9300f2b948710d2653dD7B07f33A8B32118C"),
    "USDT": _w3.to_checksum_address("0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e"),
}
_BALANCE_OF_ABI = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}]


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


async def get_onchain_celo_snapshot(db, user_id) -> dict:
    """Read this user's *actual* on-chain balances at their own derived Celo
    address — live from the chain, not the internal retail_wallets ledger.

    Read-only: derives the address from CELO_MNEMONIC + the user's HD index
    (never touches the private key beyond that), then calls balanceOf() per
    asset. Deposits are currently swept to treasury shortly after arriving
    (see workers/celo_deposit_watcher.py), so this will usually read near
    zero even for an active user — that's expected until the sweep behavior
    changes, not a bug in this function. It exists so the ledger balance can
    be shown next to what's verifiably on-chain, rather than trusted blindly.
    """
    index = await get_or_create_celo_wallet_index(db, user_id)
    address = derive_celo_account(index).address

    balances = {}
    for asset, contract_address in _ASSET_CONTRACTS.items():
        decimals = 18 if asset == "cUSD" else 6
        try:
            contract = _w3.eth.contract(address=contract_address, abi=_BALANCE_OF_ABI)
            raw = contract.functions.balanceOf(address).call()
            balances[asset] = raw / (10 ** decimals)
        except Exception as e:
            balances[asset] = None
            print(f"⚠️ Could not read on-chain {asset} balance for {address}: {e}")

    return {
        "address": address,
        "explorerUrl": f"https://celoscan.io/address/{address}",
        "balances": balances,
    }
