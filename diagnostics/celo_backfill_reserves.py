#!/usr/bin/env python3
"""Stage-2 prerequisite: back each user's ledger balance with a real on-chain
transfer to their own derived Celo address, so self-custodied withdrawals
(reading balanceOf() at that address) have something real to withdraw.

Two modes:
  --dry-run (default): report per-user shortfall and total capital required.
                        Makes no network writes, no on-chain transactions.
  --execute:            actually send the shortfall to each user's address.
                        Refuses to run at all if treasury can't cover the
                        full total for an asset — a partial backfill would
                        leave some users backed and others not, silently
                        recreating the exact confusion this is meant to fix.

Usage:
  python diagnostics/celo_backfill_reserves.py                # dry run
  python diagnostics/celo_backfill_reserves.py --execute       # real transfers
  python diagnostics/celo_backfill_reserves.py --execute --asset USDT
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

load_dotenv(override=True)

from celo_wallet import derive_celo_account, get_or_create_celo_wallet_index

CELO_RPC = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
w3 = Web3(Web3.HTTPProvider(CELO_RPC, request_kwargs={'timeout': 15}))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

ASSET_CONTRACTS = {
    "cUSD": w3.to_checksum_address("0x765DE816845861e75A25fCA122bb6898B8B1282a"),
    "USDC": w3.to_checksum_address("0xcebA9300f2b948710d2653dD7B07f33A8B32118C"),
    "USDT": w3.to_checksum_address("0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e"),
}
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
]


def get_treasury_account():
    pk = os.getenv("CELO_TREASURY_PK")
    if not pk:
        raise ValueError("CELO_TREASURY_PK is not configured.")
    return w3.eth.account.from_key(pk if pk.startswith("0x") else f"0x{pk}")


async def build_shortfall_report(db, asset_filter=None):
    """Returns {asset: [{user_id, address, ledger, onchain, shortfall}, ...]}"""
    assets = [asset_filter] if asset_filter else list(ASSET_CONTRACTS.keys())
    wallets = await db["retail_wallets"].find({}).to_list(length=100000)

    # Sum ledger balance per userId (a user can be split across rows — see
    # wallet_utils.py's get_summed_balance for the same pattern).
    ledger_by_user: dict[str, dict[str, float]] = {}
    for w in wallets:
        uid = str(w.get("userId"))
        for asset in assets:
            bal = float(w.get(asset, 0.0) or 0.0)
            if bal <= 0:
                continue
            ledger_by_user.setdefault(uid, {}).setdefault(asset, 0.0)
            ledger_by_user[uid][asset] += bal

    report: dict[str, list] = {asset: [] for asset in assets}
    for uid, asset_balances in ledger_by_user.items():
        index = await get_or_create_celo_wallet_index(db, uid)
        address = derive_celo_account(index).address
        for asset, ledger_amount in asset_balances.items():
            decimals = 18 if asset == "cUSD" else 6
            contract = w3.eth.contract(address=ASSET_CONTRACTS[asset], abi=ERC20_ABI)
            raw = contract.functions.balanceOf(address).call()
            onchain_amount = raw / (10 ** decimals)
            shortfall = round(ledger_amount - onchain_amount, 6)
            if shortfall > 0.000001:
                report[asset].append({
                    "user_id": uid, "address": address,
                    "ledger": round(ledger_amount, 6), "onchain": round(onchain_amount, 6),
                    "shortfall": shortfall,
                })
    return report


def get_treasury_balance(asset: str) -> float:
    decimals = 18 if asset == "cUSD" else 6
    contract = w3.eth.contract(address=ASSET_CONTRACTS[asset], abi=ERC20_ABI)
    raw = contract.functions.balanceOf(get_treasury_account().address).call()
    return raw / (10 ** decimals)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Actually send real transfers. Default is dry-run.")
    parser.add_argument("--asset", choices=list(ASSET_CONTRACTS.keys()), help="Limit to one asset.")
    args = parser.parse_args()

    client = AsyncIOMotorClient(os.getenv("MONGO_URL", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DB", "meshex")]

    print("Building per-user shortfall report (read-only)...\n")
    report = await build_shortfall_report(db, args.asset)

    any_shortfall = False
    for asset, rows in report.items():
        if not rows:
            print(f"{asset}: every user's ledger balance is already backed at their own address. Nothing to do.")
            continue
        any_shortfall = True
        total_needed = sum(r["shortfall"] for r in rows)
        treasury_balance = get_treasury_balance(asset)
        print(f"{asset}: {len(rows)} user(s) unbacked, total shortfall = {total_needed:.6f} {asset}")
        print(f"  Treasury currently holds: {treasury_balance:.6f} {asset}")
        for r in rows:
            print(f"    user={r['user_id']}  address={r['address']}  ledger={r['ledger']}  onchain={r['onchain']}  needs={r['shortfall']}")

        if treasury_balance < total_needed:
            print(f"  ❌ INSUFFICIENT TREASURY BALANCE. Need {total_needed - treasury_balance:.6f} more {asset} deposited into treasury before this can run.")
            if args.execute:
                print(f"  Refusing to send any {asset} transfers — a partial backfill would leave some users backed and others not.")
            print()
            continue

        print(f"  ✅ Treasury can cover the full {asset} shortfall.")
        if not args.execute:
            print(f"  (dry-run — re-run with --execute to actually send these {len(rows)} transfer(s))\n")
            continue

        # --- Real transfers from here down ---
        treasury = get_treasury_account()
        decimals = 18 if asset == "cUSD" else 6
        contract = w3.eth.contract(address=ASSET_CONTRACTS[asset], abi=ERC20_ABI)
        for r in rows:
            amount_base = int(r["shortfall"] * (10 ** decimals))
            nonce = w3.eth.get_transaction_count(treasury.address)
            tx = contract.functions.transfer(r["address"], amount_base).build_transaction({
                "chainId": 42220, "gas": 150000, "gasPrice": w3.eth.gas_price, "nonce": nonce,
            })
            signed = treasury.sign_transaction(tx)
            raw_tx = getattr(signed, "raw_transaction", getattr(signed, "rawTransaction", None))
            tx_hash = w3.to_hex(w3.eth.send_raw_transaction(raw_tx))
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            ok = receipt.get("status") == 1
            print(f"    {'✅' if ok else '❌'} sent {r['shortfall']} {asset} to {r['address']} (user {r['user_id']}) — tx {tx_hash}")
            await db["celo_backfill_log"].insert_one({
                "userId": r["user_id"], "address": r["address"], "asset": asset,
                "amount": r["shortfall"], "txHash": tx_hash, "status": "completed" if ok else "failed",
                "createdAt": datetime.utcnow(),
            })
        print()

    if not any_shortfall:
        print("\nAll users are fully backed on-chain for all checked assets. Safe to proceed to Stage 2 cutover (self-custodied withdrawal signing).")

asyncio.run(main())
