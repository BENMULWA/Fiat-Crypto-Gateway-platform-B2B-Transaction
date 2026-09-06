"""Persistent background watcher for Celo per-user deposit addresses.

Every verified user has a permanent Celo address (see celo_wallet.py) that's
shown the moment they open the deposit page — there's no "start listening"
step and no pre-committed amount, same as a Binance/Coinbase deposit address.
This watcher runs continuously, independent of any browser tab being open,
scanning new blocks for transfers into ANY registered address and crediting
whatever amount actually arrives.

On a match it:
  1. Credits the user's wallet (for the exact amount received) and records a
     completed ramp_entries row.
  2. Pushes a `deposit_credited` event over the websocket so the UI updates
     instantly instead of waiting for its next poll.
  3. Tops up the deposit address with a little CELO for gas, then sweeps its
     full token balance to the treasury so the funds are available for swaps
     and withdrawals. The sweep is best-effort: a failure here never affects
     the wallet credit the customer already received.

Each on-chain transfer is credited at most once via an atomic insert into
`celo_processed_transfers` keyed by `{tx_hash}:{log_index}` — MongoDB's
built-in `_id` uniqueness is the dedupe guard, so a rescanned block (e.g.
after a restart) can never double-credit a customer.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from pymongo.errors import DuplicateKeyError

from broadcast import broadcast_manager
from notifications import notify_user
from celo_audit import log_celo_audit_event
from celo_wallet import derive_celo_account

logger = logging.getLogger("celo.deposit_watcher")

CELO_RPC = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
POLL_INTERVAL_SECONDS = int(os.getenv("CELO_WATCHER_INTERVAL_SECONDS", "6"))
MAX_BLOCK_LOOKBACK = int(os.getenv("CELO_WATCHER_MAX_LOOKBACK_BLOCKS", "2000"))

ASSET_CONTRACTS = {
    "cUSD": "0x765DE816845861e75A25fCA122bb6898B8B1282a",
    "USDC": "0xcebA9300f2b948710d2653dD7B07f33A8B32118C",
    "USDT": "0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e",
}
ASSET_DECIMALS = {"cUSD": 18, "USDC": 6, "USDT": 6}

ERC20_ABI = [
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"anonymous": False, "inputs": [{"indexed": True, "name": "from", "type": "address"}, {"indexed": True, "name": "to", "type": "address"}, {"indexed": False, "name": "value", "type": "uint256"}], "name": "Transfer", "type": "event"},
]


def _get_treasury_account():
    pk = os.getenv("CELO_TREASURY_PK")
    if not pk:
        return None
    clean_pk = pk if pk.startswith("0x") else f"0x{pk}"
    return Web3().eth.account.from_key(clean_pk)


SCAN_CHUNK_BLOCKS = int(os.getenv("CELO_WATCHER_SCAN_CHUNK_BLOCKS", "150"))


def _get_logs_chunked(w3: Web3, checksum_address: str, transfer_topic: str, from_block: int, to_block: int) -> list:
    """Fetch logs across a wide block range in bounded chunks.

    Forno (and most RPC providers) caps eth_getLogs results — a busy contract like
    USDT can exceed that cap over just a few hundred blocks. A single-cycle scan
    normally covers a small range and needs no chunking, but catching up after any
    gap (a restart, a deploy, or the watcher falling behind) can span thousands of
    blocks, so this always chunks defensively rather than only chunking on error.
    """
    all_logs: list = []
    chunk_start = from_block
    while chunk_start <= to_block:
        chunk_end = min(chunk_start + SCAN_CHUNK_BLOCKS - 1, to_block)
        try:
            all_logs.extend(w3.eth.get_logs({
                "fromBlock": chunk_start,
                "toBlock": chunk_end,
                "address": checksum_address,
                "topics": [transfer_topic],
            }))
        except Exception as exc:
            # Shrink and retry this chunk once before giving up on it — a provider's
            # result cap can still be hit even within SCAN_CHUNK_BLOCKS under load.
            narrower_end = min(chunk_start + max(SCAN_CHUNK_BLOCKS // 5, 1) - 1, chunk_end)
            try:
                all_logs.extend(w3.eth.get_logs({
                    "fromBlock": chunk_start,
                    "toBlock": narrower_end,
                    "address": checksum_address,
                    "topics": [transfer_topic],
                }))
                chunk_end = narrower_end
            except Exception:
                logger.warning("get_logs failed for %s over %s-%s: %s", checksum_address, chunk_start, chunk_end, exc)
        chunk_start = chunk_end + 1
    return all_logs


def _scan_logs_sync(w3: Web3, from_block: int, current_block: int, watched_addresses: set[str]) -> list[dict]:
    """Blocking web3 log scan. Returns matches without touching the database."""
    matches: list[dict] = []
    transfer_topic = Web3.keccak(text="Transfer(address,address,uint256)").hex()
    if not transfer_topic.startswith("0x"):
        # Some hexbytes versions return .hex() without the "0x" prefix (a breaking
        # change from stdlib bytes.hex() semantics) — the RPC rejects a bare topic
        # hash with "invalid argument 0: hex string without 0x prefix".
        transfer_topic = "0x" + transfer_topic

    for asset, contract_address in ASSET_CONTRACTS.items():
        checksum_address = Web3.to_checksum_address(contract_address)
        logs = _get_logs_chunked(w3, checksum_address, transfer_topic, from_block, current_block)

    #  Passin the logs to the contract object to parse them into Transfer events, then filter by watched addresses.
        if not logs:
            continue

        contract = w3.eth.contract(address=checksum_address, abi=ERC20_ABI)
        for log in logs:
            try:
                parsed = contract.events.Transfer().process_log(log)
            except Exception:
                continue

            to_addr = parsed["args"]["to"].lower()
            if to_addr not in watched_addresses:
                continue

            tx_hash = parsed["transactionHash"].hex()
            if not tx_hash.startswith("0x"):
                tx_hash = "0x" + tx_hash

            matches.append({
                "asset": asset,
                "to": to_addr,
                "value": parsed["args"]["value"],
                "tx_hash": tx_hash,
                "log_index": parsed["logIndex"],
            })

    return matches


SWEEP_GAS_LIMIT = int(os.getenv("CELO_SWEEP_GAS_LIMIT", "200000"))
GAS_TOPUP_BUFFER_PCT = float(os.getenv("CELO_GAS_TOPUP_BUFFER_PCT", "0.25"))


def _sweep_deposit_sync(w3: Web3, asset: str, wallet_index: int) -> dict:
    """Top up gas from the treasury, then sweep the deposit address's full token
    balance back to the treasury. Best-effort: called after the customer's wallet
    has already been credited, so a failure here is a reconciliation task, not a
    customer-facing incident. Returns a result dict the async caller logs to the
    audit trail — this function itself stays DB-free since it runs off-thread."""
    result: dict = {"topupTxHash": None, "topupWei": 0, "sweepTxHash": None, "sweptRawAmount": 0}

    treasury = _get_treasury_account()
    if not treasury:
        logger.warning("CELO_TREASURY_PK not configured; skipping sweep for index=%s", wallet_index)
        return result

    deposit_account = derive_celo_account(wallet_index)
    contract = w3.eth.contract(address=Web3.to_checksum_address(ASSET_CONTRACTS[asset]), abi=ERC20_ABI)

    # 1. Gas top-up sized from the LIVE gas price, not a fixed guess — a fixed
    #    fixed top-up amount silently under-funds the sweep whenever gas price
    #    rises (this is exactly what happened on 2026-09-03: 0.01 CELO topped up
    #    against a sweep that actually needed ~0.0405 CELO at the time).
    gas_price = w3.eth.gas_price
    required_wei = int(gas_price * SWEEP_GAS_LIMIT * (1 + GAS_TOPUP_BUFFER_PCT))
    existing_balance_wei = w3.eth.get_balance(deposit_account.address)
    topup_wei = max(required_wei - existing_balance_wei, 0)

    if topup_wei > 0:
        treasury_balance_wei = w3.eth.get_balance(treasury.address)
        if treasury_balance_wei < topup_wei:
            raise RuntimeError(
                f"Treasury has {Web3.from_wei(treasury_balance_wei, 'ether')} CELO, "
                f"needs {Web3.from_wei(topup_wei, 'ether')} CELO to fund this sweep's gas top-up."
            )

        gas_tx = {
            "from": treasury.address,
            "to": deposit_account.address,
            "value": topup_wei,
            "nonce": w3.eth.get_transaction_count(treasury.address),
            "gas": 21000,
            "gasPrice": gas_price,
            "chainId": w3.eth.chain_id,
        }
        signed_gas_tx = treasury.sign_transaction(gas_tx)
        raw_gas_tx = getattr(signed_gas_tx, "raw_transaction", getattr(signed_gas_tx, "rawTransaction", None))
        gas_tx_hash = w3.eth.send_raw_transaction(raw_gas_tx)
        w3.eth.wait_for_transaction_receipt(gas_tx_hash, timeout=60)
        result["topupTxHash"] = gas_tx_hash.hex()
        result["topupWei"] = topup_wei

    # 2. Sweep the full on-chain balance (covers any prior dust plus this deposit).
    balance = contract.functions.balanceOf(deposit_account.address).call()
    if balance <= 0:
        return result

    sweep_tx = contract.functions.transfer(treasury.address, balance).build_transaction({
        "from": deposit_account.address,
        "nonce": w3.eth.get_transaction_count(deposit_account.address),
        "gas": SWEEP_GAS_LIMIT,
        "gasPrice": gas_price,
        "chainId": w3.eth.chain_id,
    })
    signed_sweep_tx = deposit_account.sign_transaction(sweep_tx)
    raw_sweep_tx = getattr(signed_sweep_tx, "raw_transaction", getattr(signed_sweep_tx, "rawTransaction", None))
    sweep_tx_hash = w3.eth.send_raw_transaction(raw_sweep_tx)
    logger.info("Swept %s (raw units) %s from index=%s to treasury: %s", balance, asset, wallet_index, sweep_tx_hash.hex())
    result["sweepTxHash"] = sweep_tx_hash.hex()
    result["sweptRawAmount"] = balance
    return result


async def _process_scan(db, w3: Web3):
    index_docs = await db["celo_wallet_indexes"].find({}, {"_id": 1, "userId": 1, "index": 1}).to_list(length=20000)
    if not index_docs:
        return

    by_address: dict[str, dict] = {}
    for doc in index_docs:
        try:
            address = derive_celo_account(doc["index"]).address.lower()
        except Exception:
            logger.warning("Could not derive address for wallet index doc %s", doc.get("_id"))
            continue
        by_address[address] = doc

    watermark_doc = await db["chain_watermarks"].find_one({"_id": "celo"})
    current_block = w3.eth.block_number
    default_from = current_block - 200  # ~15 min of Celo blocks (5s block time)
    from_block = max((watermark_doc or {}).get("lastBlock", default_from), current_block - MAX_BLOCK_LOOKBACK)

    matches = await asyncio.to_thread(_scan_logs_sync, w3, from_block, current_block, set(by_address.keys()))

    for match in matches:
        owner = by_address.get(match["to"])
        if not owner:
            continue

        decimals = ASSET_DECIMALS[match["asset"]]
        received_amount = match["value"] / (10 ** decimals)
        if received_amount <= 0:
            continue

        # Atomic dedupe: MongoDB's _id uniqueness ensures only the first time we
        # see this exact transfer gets processed, even across watcher restarts.
        dedupe_id = f"{match['tx_hash']}:{match['log_index']}"
        try:
            await db["celo_processed_transfers"].insert_one({"_id": dedupe_id, "processedAt": datetime.utcnow()})
        except DuplicateKeyError:
            continue

        user_id = owner["userId"]
        wallet_index = owner["index"]
        await db["retail_wallets"].update_one(
            {"userId": user_id},
            {"$inc": {match["asset"]: received_amount}},
            upsert=True,
        )

        now = datetime.utcnow()
        await db["ramp_entries"].insert_one({
            "_id": f"TRADE_{uuid.uuid4().hex[:8].upper()}",
            "direction": "on",
            "channel": "Celo Auto-Detect",
            "fromAsset": match["asset"],
            "toAsset": match["asset"],
            "fromAmount": received_amount,
            "toAmount": received_amount,
            "status": "completed",
            "userId": user_id,
            "celoTxHash": match["tx_hash"],
            "depositAddress": match["to"],
            "createdAt": now,
            "date": now.strftime("%b %d, %Y"),
            "timeAgo": "Just now",
        })

        try:
            await broadcast_manager.send_user(str(user_id), {
                "type": "deposit_credited",
                "userId": str(user_id),
                "asset": match["asset"],
                "amount": received_amount,
                "txHash": match["tx_hash"],
                "network": "celo",
            })
        except Exception:
            logger.debug("Broadcast failed for tx %s (non-fatal)", match["tx_hash"])

        await notify_user(
            db, user_id, "deposit", "success",
            "Deposit received",
            f"{received_amount:g} {match['asset']} arrived on Celo and was credited to your account.",
            extra={"asset": match["asset"], "amount": received_amount, "txHash": match["tx_hash"]},
        )

        try:
            sweep_result = await asyncio.to_thread(_sweep_deposit_sync, w3, match["asset"], wallet_index)
            await log_celo_audit_event(
                db, "sweep_completed",
                userId=str(user_id), asset=match["asset"], depositTxHash=match["tx_hash"],
                depositAddress=match["to"], walletIndex=wallet_index,
                topupTxHash=sweep_result.get("topupTxHash"), topupWei=sweep_result.get("topupWei"),
                sweepTxHash=sweep_result.get("sweepTxHash"), sweptRawAmount=sweep_result.get("sweptRawAmount"),
            )
        except Exception as exc:
            logger.exception("Sweep failed for tx %s — funds remain safely on the deposit address", match["tx_hash"])
            await log_celo_audit_event(
                db, "sweep_failed",
                userId=str(user_id), asset=match["asset"], depositTxHash=match["tx_hash"],
                depositAddress=match["to"], walletIndex=wallet_index, error=str(exc),
            )

    await db["chain_watermarks"].update_one(
        {"_id": "celo"},
        {"$set": {"lastBlock": current_block, "updatedAt": datetime.utcnow()}},
        upsert=True,
    )


async def celo_deposit_watcher_loop(db, stop_event: asyncio.Event | None = None):
    stop_event = stop_event or asyncio.Event()
    w3 = Web3(Web3.HTTPProvider(CELO_RPC, request_kwargs={"timeout": 15}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    logger.info("Starting Celo deposit watcher (interval=%ss)", POLL_INTERVAL_SECONDS)
    try:
        while not stop_event.is_set():
            try:
                await _process_scan(db, w3)
            except Exception:
                logger.exception("Unexpected error in Celo deposit watcher iteration")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                continue
    finally:
        logger.info("Celo deposit watcher stopping")
