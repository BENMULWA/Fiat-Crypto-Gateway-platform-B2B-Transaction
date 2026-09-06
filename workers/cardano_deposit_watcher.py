"""Persistent background watcher for Cardano per-user deposit addresses.

Mirrors workers/celo_deposit_watcher.py's design: every verified user gets a
permanent Cardano address (see cardano_child_wallet.py), shown the moment
they open the deposit page — no session, no pre-committed amount. This
watcher runs continuously, independent of any browser tab, and credits
whatever USDA actually arrives.

Cardano has no "event log" the way EVM chains do, so detection here works by
diffing each address's current on-chain USDA balance (via Blockfrost) against
the last-known balance recorded for it — an increase is a deposit. This means
one Blockfrost call per registered address per cycle, unlike Celo's constant
number of calls regardless of user count; if the user base grows large this
should move to Blockfrost's webhook feature instead of polling.

On a detected increase it:
  1. Credits the user's wallet for the delta and records a completed
     ramp_entries row.
  2. Pushes a `deposit_credited` event over the websocket.
  3. Tops up the child address with a little ADA if it doesn't have enough to
     cover its own sweep transaction fee, then sweeps its full USDA + ADA
     balance back to the treasury master wallet. Best-effort — a failure here
     never affects the wallet credit the customer already received.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime

from pymongo.errors import DuplicateKeyError

from broadcast import broadcast_manager
from notifications import notify_user
from cardano_child_wallet import derive_cardano_child_account

logger = logging.getLogger("cardano.deposit_watcher")

# Tightened from 30s -> 10s so on-chain confirmation (~20s Cardano block time)
# doesn't stack with a slow poll cadence on top of it. Each cycle does one
# Blockfrost call per registered address, so this scales with active users —
# revisit (or move to Blockfrost webhooks) if the user base grows large enough
# to threaten Blockfrost's rate limit at this interval.
POLL_INTERVAL_SECONDS = int(os.getenv("CARDANO_WATCHER_INTERVAL_SECONDS", "10"))
ADA_TOPUP_LOVELACE = int(os.getenv("CARDANO_ADA_TOPUP_LOVELACE", "2000000"))  # 2 ADA default
MIN_ADA_FOR_SWEEP_LOVELACE = int(os.getenv("CARDANO_MIN_ADA_FOR_SWEEP_LOVELACE", "1500000"))  # 1.5 ADA


def _import_usda_ops():
    """cardano-USDA has a hyphen in its directory name, so it can't be a normal
    package import — reuse the same dynamic-load trick as cardano/client.py."""
    import importlib.util
    import sys
    from pathlib import Path

    current_dir = Path(__file__).resolve().parent.parent
    src = current_dir / "cardano-USDA" / "usda.py"
    spec = importlib.util.spec_from_file_location("cardano_usda_ops", str(src))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_usda_ops = _import_usda_ops()


def _get_treasury_wallet():
    from cardano.wallet import CardanoWallet
    return CardanoWallet()  # ignores index — this is the shared custodial master wallet


def _sweep_deposit_sync(wallet_index: int) -> dict:
    """Top up ADA if needed, then sweep the child address's full ADA + USDA
    balance to the treasury master wallet. Returns a result dict the async
    caller logs — this function stays DB-free since it runs off-thread."""
    from pycardano import TransactionBuilder, TransactionOutput, Value

    from cardano.client import get_chain_context

    result: dict = {"topupTxHash": None, "topupLovelace": 0, "sweepTxHash": None}

    signing_key, child_address = derive_cardano_child_account(wallet_index)
    treasury = _get_treasury_wallet()
    context = get_chain_context()

    child_balance = _usda_ops.get_balance(str(child_address))
    child_lovelace = int(child_balance["ada"] * 1_000_000)

    if child_lovelace < MIN_ADA_FOR_SWEEP_LOVELACE:
        topup_needed = ADA_TOPUP_LOVELACE
        topup_builder = TransactionBuilder(context)
        topup_builder.add_input_address(treasury.address)
        topup_builder.add_output(TransactionOutput(child_address, Value(coin=topup_needed)))
        signed_topup = topup_builder.build_and_sign(
            signing_keys=[treasury.signing_key], change_address=treasury.address,
        )
        context.submit_tx(signed_topup.to_cbor())
        result["topupTxHash"] = str(signed_topup.id)
        result["topupLovelace"] = topup_needed

    # Re-check balance (fresh UTXOs after any top-up) then sweep everything —
    # zero explicit outputs + change_address=treasury sends the full input
    # value (minus fee) to the treasury in one go, covering ADA and USDA alike.
    sweep_builder = TransactionBuilder(context)
    sweep_builder.add_input_address(child_address)
    signed_sweep = sweep_builder.build_and_sign(
        signing_keys=[signing_key], change_address=treasury.address,
    )
    context.submit_tx(signed_sweep.to_cbor())
    result["sweepTxHash"] = str(signed_sweep.id)
    return result


async def _process_scan(db):
    index_docs = await db["cardano_child_wallet_indexes"].find({}, {"_id": 1, "userId": 1, "index": 1}).to_list(length=20000)
    if not index_docs:
        return

    for doc in index_docs:
        wallet_index = doc["index"]
        user_id = doc["userId"]

        try:
            _, address = await asyncio.to_thread(derive_cardano_child_account, wallet_index)
            balance = await asyncio.to_thread(_usda_ops.get_balance, str(address))
        except Exception:
            logger.exception("Failed to check balance for wallet index=%s", wallet_index)
            continue

        usda_raw = balance["usda_raw"]
        if usda_raw <= 0:
            continue

        watermark_doc = await db["cardano_deposit_watermarks"].find_one({"_id": str(address)})
        last_usda_raw = (watermark_doc or {}).get("lastUsdaRaw", 0)

        if usda_raw <= last_usda_raw:
            continue  # no net-new inbound funds since last check

        delta_raw = usda_raw - last_usda_raw
        delta_usda = _usda_ops._to_usda(delta_raw)

        # Atomic claim on the watermark update: only the first cycle to see this
        # exact balance level gets to credit it, even under concurrent scans.
        claim = await db["cardano_deposit_watermarks"].find_one_and_update(
            {"_id": str(address), "lastUsdaRaw": last_usda_raw},
            {"$set": {"lastUsdaRaw": usda_raw, "updatedAt": datetime.utcnow()}},
            upsert=not watermark_doc,
        )
        if watermark_doc and not claim:
            continue  # another cycle already claimed this increase

        await db["retail_wallets"].update_one(
            {"userId": user_id},
            {"$inc": {"USDA": delta_usda}},
            upsert=True,
        )

        # Best-effort: find the specific tx hash responsible, for the user-facing
        # explorer link — balance-diffing doesn't hand us one directly the way
        # Celo's event-log scan does.
        tx_hash = None
        try:
            recent_txs = await asyncio.to_thread(_usda_ops.get_usda_transactions, str(address), 5)
            for tx in recent_txs:
                if tx.get("direction") == "receive":
                    tx_hash = tx.get("tx_hash")
                    break
        except Exception:
            logger.debug("Could not resolve tx hash for deposit at %s (non-fatal)", address)

        now = datetime.utcnow()
        entry_id = f"TRADE_{uuid.uuid4().hex[:8].upper()}"
        await db["ramp_entries"].insert_one({
            "_id": entry_id,
            "direction": "on",
            "channel": "Cardano Auto-Detect",
            "fromAsset": "USDA",
            "toAsset": "USDA",
            "fromAmount": delta_usda,
            "toAmount": delta_usda,
            "status": "completed",
            "userId": user_id,
            "depositAddress": str(address),
            "cardanoTxHash": tx_hash,
            "createdAt": now,
            "date": now.strftime("%b %d, %Y"),
            "timeAgo": "Just now",
        })

        try:
            await broadcast_manager.send_user(str(user_id), {
                "type": "deposit_credited",
                "userId": str(user_id),
                "asset": "USDA",
                "amount": delta_usda,
                "network": "cardano",
                "txHash": tx_hash,
            })
        except Exception:
            logger.debug("Broadcast failed for user %s (non-fatal)", user_id)

        await notify_user(
            db, user_id, "deposit", "success",
            "Deposit received",
            f"{delta_usda:g} USDA arrived on Cardano and was credited to your account.",
            extra={"asset": "USDA", "amount": delta_usda, "txHash": tx_hash},
        )

        try:
            sweep_result = await asyncio.to_thread(_sweep_deposit_sync, wallet_index)
            logger.info("Swept wallet index=%s: %s", wallet_index, sweep_result)
        except Exception:
            logger.exception("Sweep failed for wallet index=%s — funds remain safely on the deposit address", wallet_index)


async def cardano_deposit_watcher_loop(db, stop_event: asyncio.Event | None = None):
    stop_event = stop_event or asyncio.Event()
    logger.info("Starting Cardano deposit watcher (interval=%ss)", POLL_INTERVAL_SECONDS)
    try:
        while not stop_event.is_set():
            try:
                await _process_scan(db)
            except Exception:
                logger.exception("Unexpected error in Cardano deposit watcher iteration")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                continue
    finally:
        logger.info("Cardano deposit watcher stopping")
