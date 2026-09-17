"""Runs a real HFTCorridorFSM (Brain_Engine/state_engine.py) as a
long-lived background task instead of blocking an HTTP request for the
run's entire lifetime — which can now legitimately be minutes to a day,
since ROLLOVER hands off to AWAITING_OPPORTUNITY instead of looping
immediately (see state_engine.py).

Each run's live state (cycle, principal, KES float, FSMState) is persisted
to the `corridor_runs` collection after every tick, not just its completed
ledger legs — so a run can be reconstructed and resumed if the process
restarts mid-hold. This is the actual gap the old synchronous
/corridor/execute-hft endpoint had: that endpoint's in-memory FSM object
was the *only* copy of "which cycle are we on / how much principal do we
have," and it vanished the instant the request ended or the process died.

Only one real corridor task per run_id runs in this process at a time —
_active_tasks tracks that so a duplicate /corridor/start (or the
startup-time resume sweep racing a request that just started the same run)
can't launch two FSMs against the same run_id concurrently.
"""
from __future__ import annotations

import logging
import traceback
from datetime import datetime
from typing import Any, Optional

import asyncio

from Brain_Engine.node_registry import CORRIDORS, corridor_eligible
from Brain_Engine.state_engine import HFTCorridorFSM, ImmutableLedger, FSMState

logger = logging.getLogger("treasury.corridor_worker")

RUNS_COLLECTION = "corridor_runs"
TERMINAL_STATES = {FSMState.COMPLETED.value, FSMState.HALTED.value}

_active_tasks: dict[str, asyncio.Task] = {}


async def start_corridor_run(db, corridor_id: str, amount_usd: float, started_by: Optional[str] = None) -> dict[str, Any]:
    """Creates the persisted run record and launches its background task.
    Returns the run doc immediately — the caller does not wait for a
    single tick, let alone the whole run, to complete."""
    if corridor_id not in CORRIDORS:
        raise ValueError(f"Unknown corridor ID: {corridor_id!r}")
    if not corridor_eligible(corridor_id):
        raise ValueError(
            f"Corridor {corridor_id!r} is disabled or one of its nodes is switched off — "
            "re-enable it via /api/imm before starting a live run."
        )
    if amount_usd <= 0:
        raise ValueError("Amount must be greater than zero.")

    corridor = CORRIDORS[corridor_id]
    run_id = f"RUN-{corridor_id.upper()}-{int(datetime.utcnow().timestamp() * 1000)}"
    now = datetime.utcnow()

    run_doc = {
        "_id": run_id,
        "corridorId": corridor_id,
        "config": {
            "cycles": 5,
            "discount": corridor["discount"],
            "fx_edge": corridor["fx_edge"],
            "node_procure": corridor["node_procure"],
            "node_liquidate": corridor["node_liquidate"],
        },
        "status": FSMState.IDLE.value,
        "currentCycle": 1,
        "currentUsdPrincipal": amount_usd,
        "currentKesFloat": 0.0,
        "startingUsd": amount_usd,
        "finalUsd": None,
        "profit": None,
        "haltReason": None,
        "startedBy": started_by,
        "createdAt": now,
        "updatedAt": now,
    }
    await db[RUNS_COLLECTION].insert_one(run_doc)

    task = asyncio.create_task(_drive_run(db, run_id))
    _active_tasks[run_id] = task
    task.add_done_callback(lambda t, rid=run_id: _active_tasks.pop(rid, None))

    return run_doc


async def get_run_status(db, run_id: str) -> Optional[dict[str, Any]]:
    return await db[RUNS_COLLECTION].find_one({"_id": run_id})


async def list_runs(db, limit: int = 25) -> list[dict[str, Any]]:
    cursor = db[RUNS_COLLECTION].find({}).sort("createdAt", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def resume_pending_corridor_runs(db) -> int:
    """Called once at server startup: relaunches any run left in a
    non-terminal state by a previous process (crash, deploy, restart) from
    exactly the cycle/principal/state it last persisted. Returns how many
    were resumed."""
    resumed = 0
    cursor = db[RUNS_COLLECTION].find({"status": {"$nin": list(TERMINAL_STATES)}})
    async for run_doc in cursor:
        run_id = run_doc["_id"]
        if run_id in _active_tasks:
            continue
        logger.info("Resuming corridor run %s from cycle %s, state %s",
                    run_id, run_doc.get("currentCycle"), run_doc.get("status"))
        task = asyncio.create_task(_drive_run(db, run_id))
        _active_tasks[run_id] = task
        task.add_done_callback(lambda t, rid=run_id: _active_tasks.pop(rid, None))
        resumed += 1
    return resumed


def _build_fsm(ledger: ImmutableLedger, run_doc: dict[str, Any]) -> HFTCorridorFSM:
    config = dict(run_doc["config"])
    config["run_id"] = run_doc["_id"]
    config["resume_cycle"] = run_doc["currentCycle"]
    config["resume_principal_usd"] = run_doc["currentUsdPrincipal"]
    config["resume_kes_float"] = run_doc.get("currentKesFloat", 0.0)
    config["resume_state"] = run_doc["status"]
    return HFTCorridorFSM(ledger, starting_capital_usd=run_doc["startingUsd"], config=config)


async def _persist_tick(db, run_id: str, bot: HFTCorridorFSM) -> None:
    update: dict[str, Any] = {
        "status": bot.state.value,
        "currentCycle": bot.current_cycle,
        "currentUsdPrincipal": bot.current_usd_principal,
        "currentKesFloat": bot.current_kes_float,
        "updatedAt": datetime.utcnow(),
    }
    if bot.state == FSMState.HALTED and bot.halt_reason:
        update["haltReason"] = bot.halt_reason
    if bot.state == FSMState.COMPLETED:
        update["finalUsd"] = bot.current_usd_principal
        # startingUsd is immutable once the run is created — read fresh
        # from the persisted doc rather than trusting a local variable that
        # could be stale after a resume.
        run_doc = await db[RUNS_COLLECTION].find_one({"_id": run_id}, {"startingUsd": 1})
        starting_usd = (run_doc or {}).get("startingUsd", bot.current_usd_principal)
        update["profit"] = bot.current_usd_principal - starting_usd
    await db[RUNS_COLLECTION].update_one({"_id": run_id}, {"$set": update})


async def _drive_run(db, run_id: str) -> None:
    """The actual long-lived task: reconstructs the FSM from its last
    persisted state, then ticks it to completion (or a halt), persisting
    after every single tick — including the ticks spent quietly holding in
    AWAITING_OPPORTUNITY, so a restart mid-hold resumes the hold, not a
    fresh cycle."""
    run_doc = await db[RUNS_COLLECTION].find_one({"_id": run_id})
    if not run_doc:
        logger.warning("Corridor run %s vanished before its worker could start", run_id)
        return

    ledger = ImmutableLedger(db_collection=db["transactions"])

    try:
        bot = _build_fsm(ledger, run_doc)
    except Exception as e:
        logger.error("Corridor run %s failed to reconstruct: %s", run_id, e)
        await db[RUNS_COLLECTION].update_one(
            {"_id": run_id},
            {"$set": {"status": FSMState.HALTED.value, "haltReason": str(e), "updatedAt": datetime.utcnow()}},
        )
        return

    if bot.state == FSMState.IDLE:
        await bot.boot_system()  # only the very first tick of a fresh run funds N7

    try:
        while bot.state not in (FSMState.COMPLETED, FSMState.HALTED):
            await bot.tick()
            await _persist_tick(db, run_id, bot)
    except Exception as e:
        traceback.print_exc()
        logger.error("Corridor run %s crashed mid-tick: %s", run_id, e)
        await db[RUNS_COLLECTION].update_one(
            {"_id": run_id},
            {"$set": {"status": FSMState.HALTED.value, "haltReason": str(e), "updatedAt": datetime.utcnow()}},
        )
        return

    logger.info("Corridor run %s finished: %s (cycle %s, $%.4f)",
                run_id, bot.state.value, bot.current_cycle, bot.current_usd_principal)
