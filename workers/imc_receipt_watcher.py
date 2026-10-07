"""Background listener for IMC arriving at the treasury wallet.

Keeps the imc_receipts records current so the console can show what was received and a corridor
run can claim a transfer the moment it lands. It reads the chain only; it moves nothing."""
from __future__ import annotations

import asyncio
import logging

from services.imc_receipts import ImcReceipts

logger = logging.getLogger("workers.imc_receipt_watcher")


async def imc_receipt_watcher_loop(db, stop: asyncio.Event, interval_seconds: float = 15.0) -> None:
    svc = ImcReceipts(db)
    while not stop.is_set():
        try:
            created = await svc.scan()
            if created:
                logger.info("Recorded %s new IMC transfer(s) to the treasury", created)
        except Exception as exc:  # keep listening through RPC hiccups
            logger.warning("IMC scan failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
