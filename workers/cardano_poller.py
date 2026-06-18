"""Background poller for Cardano deposits and confirmations.

This is a lightweight skeleton that:
- polls DB for pending `ramp_entries` with a `cardanoTxHash` and attempts to verify them
- updates `ramp_entries` and `ledger_entries` on successful verification
- can be started as a background task from FastAPI startup or run as a separate worker process

Notes:
- It uses the same `usda_ops.verify_deposit` helper as the API route so behaviour is consistent.
- For production, run this with a process supervisor (systemd, docker service, celery, rq) and monitor logs.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from ..config import settings
from ..database import get_db

logger = logging.getLogger("cardano.poller")

POLL_INTERVAL = int(getattr(settings, "cardano_poller_interval_seconds", 30))


async def _verify_pending_entries(db: Any):
    """Find pending ramp entries with cardanoTxHash and try to verify them."""
    usda_ops = None
    try:
        from cardano.usda import verify_deposit  # lazy import
        usda_ops = verify_deposit
    except Exception as exc:
        logger.debug("Cardano ops not available: %s", exc)
        return

    cursor = db.ramp_entries.find({"status": {"$in": ["pending", "watching"]}, "cardanoTxHash": {"$exists": True}})
    async for doc in cursor:
        tx_hash = doc.get("cardanoTxHash")
        workspace = doc.get("workspaceId")
        addr = doc.get("cardanoAddress")
        if not tx_hash or not addr:
            continue

        logger.info("Attempting verify for tx %s (workspace=%s)", tx_hash, workspace)
        try:
            result = usda_ops(tx_hash, addr)
        except ValueError as ve:
            logger.info("Verification failed (not valid deposit yet): %s", ve)
            continue
        except Exception as exc:
            logger.exception("Error verifying tx %s: %s", tx_hash, exc)
            continue

        # If verify_deposit returns successfully, result expected to contain usda_amount
        usda_amount = result.get("usda_amount")
        now = datetime.utcnow()

        await db.ramp_entries.update_one(
            {"_id": doc["_id"]},
            {"$set": {"status": "on", "verifiedAt": now, "usdaReceived": usda_amount}},
        )

        ledger_doc = {
            "date": now.strftime("%-d %b, %I:%M %p"),
            "flow": "On-Ramp",
            "description": f"Cardano On-Ramp: received {usda_amount:.6f} USDA — tx {tx_hash[:16]}…",
            "debitWallet": "USDA Wallet",
            "creditWallet": "Cardano",
            "debitAmount": usda_amount,
            "creditAmount": usda_amount,
            "debitAsset": "USDA",
            "creditAsset": "USDA",
            "counterparty": doc.get("counterparty") or "Cardano On-Chain",
            "workspaceId": workspace,
            "userId": doc.get("userId"),
            "createdAt": now,
        }
        await db.ledger_entries.insert_one(ledger_doc)

        logger.info("Marked tx %s as verified and created ledger entry", tx_hash)


async def poller_loop(stop_event: asyncio.Event | None = None):
    """Main poller loop. Call `await poller_loop()` from an async entrypoint.

    If `stop_event` is provided it will be used to break the loop for graceful shutdown.
    """
    # We assume this file is executed with CWD set to backend so relative imports work.
    # Acquire a short-lived DB client for polling.
    db = await get_db()

    stop_event = stop_event or asyncio.Event()

    logger.info("Starting Cardano poller (interval=%s)s", POLL_INTERVAL)

    try:
        while not stop_event.is_set():
            try:
                await _verify_pending_entries(db)
            except Exception:
                logger.exception("Unexpected error in cardano poller iteration")

            # Wait with cancellation support
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL)
            except asyncio.TimeoutError:
                continue
    finally:
        logger.info("Cardano poller stopping")


# Helper to start as a background task from FastAPI app
def start_in_background(loop: asyncio.AbstractEventLoop):
    """Schedule the poller loop on the given event loop."""
    stop_evt = asyncio.Event()

    async def _runner():
        await poller_loop(stop_evt)

    task = loop.create_task(_runner())
    return task, stop_evt
