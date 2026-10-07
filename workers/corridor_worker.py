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
from datetime import datetime, timedelta
from typing import Any, Optional

import asyncio
import uuid

from Brain_Engine.node_registry import CORRIDORS, corridor_eligible
from Brain_Engine.state_engine import HFTCorridorFSM, ImmutableLedger, FSMState

logger = logging.getLogger("treasury.corridor_worker")

RUNS_COLLECTION = "corridor_runs"
TERMINAL_STATES = {FSMState.COMPLETED.value, FSMState.HALTED.value}

# Who drives a run. The database is shared by every server that has this .env (a second machine, a leftover terminal),
# and two servers driving one real-money run could both swap or mint. A run is claimed by one server at a time: the
# owner stamps a heartbeat on every step, and another server may only take it over once that heartbeat has gone stale.
PROCESS_ID = uuid.uuid4().hex
LEASE_SECONDS = 180
LEASE_RETRY_SECONDS = 15

_active_tasks: dict[str, asyncio.Task] = {}


MAX_CYCLES = 10
PROCURE_MODES = {"stk", "existing_float"}
SWAP_MODES = {"reserve", "pool"}


async def start_corridor_run(
    db, corridor_id: str, amount_usd: float, started_by: Optional[str] = None, cycles: int = 5,
    procure_mode: str = "stk", swap_mode: str = "reserve", pool_max_slippage: float = 0.02,
) -> dict[str, Any]:
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
    # cycles=1 runs procure -> liquidate -> mint -> exit once, with no rollover:
    # the FSM goes straight to the exit when current_cycle >= max cycles.
    if not isinstance(cycles, int) or isinstance(cycles, bool) or not 1 <= cycles <= MAX_CYCLES:
        raise ValueError(f"Cycles must be a whole number from 1 to {MAX_CYCLES}.")

    if procure_mode not in PROCURE_MODES:
        raise ValueError(f"procure_mode must be one of {sorted(PROCURE_MODES)}.")

    if isinstance(pool_max_slippage, bool) or not isinstance(pool_max_slippage, (int, float)) or not 0.001 <= pool_max_slippage <= 0.50:
        raise ValueError("pool_max_slippage must be between 0.1% and 50%.")
    if swap_mode not in SWAP_MODES:
        raise ValueError(f"swap_mode must be one of {sorted(SWAP_MODES)}.")

    corridor = CORRIDORS[corridor_id]
    run_id = f"RUN-{corridor_id.upper()}-{int(datetime.utcnow().timestamp() * 1000)}"
    now = datetime.utcnow()

    run_doc = {
        "_id": run_id,
        "corridorId": corridor_id,
        "config": {
            "cycles": cycles,
            "procure_mode": procure_mode,
            "swap_mode": swap_mode,
            "pool_max_slippage": float(pool_max_slippage),
            "discount": corridor["discount"],
            "fx_edge": corridor["fx_edge"],
            "node_procure": corridor["node_procure"],
            "node_liquidate": corridor["node_liquidate"],
            # Corridor-defined provider variant (node_registry.CORRIDORS) —
            # a corridor with neither key omits them entirely, so
            # HFTCorridorFSM's own "cardano"/"native" defaults apply,
            # exactly as every pre-existing corridor already behaves.
            **({"mint_provider": corridor["mint_provider"]} if "mint_provider" in corridor else {}),
            **({"exit_provider": corridor["exit_provider"]} if "exit_provider" in corridor else {}),
        },
        "status": FSMState.IDLE.value,
        "currentCycle": 1,
        "currentUsdPrincipal": amount_usd,
        "currentKesFloat": 0.0,
        "startingUsd": amount_usd,
        "finalUsd": None,
        "profit": None,
        "haltReason": None,
        "exitPathChosen": None,
        "cyclePnl": 0.0,
        "cumulativePnl": 0.0,
        "startedBy": started_by,
        "createdAt": now,
        "updatedAt": now,
    }
    await db[RUNS_COLLECTION].insert_one(run_doc)

    task = asyncio.create_task(_drive_run(db, run_id))
    _active_tasks[run_id] = task
    task.add_done_callback(lambda t, rid=run_id: _active_tasks.pop(rid, None))

    return run_doc


async def cancel_corridor_run(db, run_id: str, cancelled_by: Optional[str] = None) -> dict[str, Any]:
    """Stops a run that is not finished and marks it HALTED, so it no longer holds the
    float or resumes after a restart. This only stops FURTHER steps: anything the run already
    did (an STK push, a mint, a swap) is real and is not reversed.

    Raises LookupError for an unknown run and ValueError for one that already finished."""
    run = await db[RUNS_COLLECTION].find_one({"_id": run_id})
    if not run:
        raise LookupError(f"Unknown corridor run ID: {run_id}")
    if run.get("status") in TERMINAL_STATES:
        raise ValueError(f"Run {run_id} has already finished ({run.get('status')}).")

    # Record the cancel FIRST. Any worker still alive for this run (this process's task, or one that survived a
    # reload) sees it before its next step and stops, and its status writes are refused from now on.
    stamp = datetime.utcnow()
    await db[RUNS_COLLECTION].update_one({"_id": run_id}, {"$set": {"cancelledAt": stamp, "cancelledBy": cancelled_by}})

    # Then stop the background task and wait for it.
    task = _active_tasks.get(run_id)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    else:
        logger.warning("Cancelling %s: this process has no live task for it. Another worker may still be driving it; it will stop at its next step.", run_id)

    try:
        from services.imc_receipts import ImcReceipts
        await ImcReceipts(db).release_run(run_id)  # free any IMC it had reserved but not booked
    except Exception as e:  # never block a cancel on this
        logger.warning("Could not release IMC reserved by %s: %s", run_id, e)

    now = datetime.utcnow()
    note = f"Cancelled by an operator while {run.get('status')} (cycle {run.get('currentCycle')})."
    await db[RUNS_COLLECTION].update_one(
        {"_id": run_id},
        {"$set": {
            "status": FSMState.HALTED.value, "haltReason": note,
            "cancelledBy": cancelled_by, "cancelledAt": now, "updatedAt": now,
            "pendingTopupExpectedKes": None, "pendingMintExpectedImc": None,
        }},
    )
    return effective_run(await db[RUNS_COLLECTION].find_one({"_id": run_id}))


async def retry_exit(db, run_id: str, requested_by: Optional[str] = None, balance_fn=None) -> dict[str, Any]:
    """Resumes a pool run that stopped at its final withdrawal: the IMC was already sold for USDC, the USDC sits in
    the engine's Comet wallet, and only the withdrawal to the exit wallet is left. Re-runs just that step.

    Refuses unless the run is HALTED, has a recorded swap and no recorded withdrawal, and the engine wallet still
    holds the USDC (so a withdrawal that did go through is never repeated).
    Raises LookupError for an unknown run and ValueError for anything that cannot be retried."""
    run = await db[RUNS_COLLECTION].find_one({"_id": run_id})
    if not run:
        raise LookupError(f"Unknown corridor run ID: {run_id}")
    if run.get("status") != FSMState.HALTED.value:
        raise ValueError(f"Run {run_id} is {run.get('status')}, not stopped. Only a stopped run can be retried.")
    swap, config = run.get("swap"), run.get("config") or {}
    if config.get("swap_mode") != "pool" or not swap or not swap.get("outAmount"):
        raise ValueError("This run has no recorded pool swap, so there is no withdrawal to retry.")
    if run.get("exit"):
        raise ValueError("This run's withdrawal already went through.")
    if run_id in _active_tasks and not _active_tasks[run_id].done():
        raise ValueError("This run is still working.")

    amount, wallet = float(swap["outAmount"]), swap.get("engineWallet")
    if not wallet:
        raise ValueError("The run did not record which wallet holds the USDC.")
    if balance_fn is None:
        from Brain_Engine.state_engine import _real_celo_token_balance_of as balance_fn
    held = await asyncio.to_thread(balance_fn, str(swap.get("outSymbol") or "USDC"), wallet)
    if held + 1e-6 < amount:
        raise ValueError(f"The engine wallet holds {held:.6f} USDC but this run's withdrawal needs {amount:.6f}. It may already have been withdrawn.")

    now = datetime.utcnow()
    await db[RUNS_COLLECTION].update_one(
        {"_id": run_id},
        {"$set": {"status": FSMState.CELO_EXIT.value, "haltReason": None, "currentUsdPrincipal": amount,
                  "exitSwapBooked": True, "exitRetriedBy": requested_by, "exitRetriedAt": now, "updatedAt": now},
         "$unset": {"cancelledAt": "", "cancelledBy": ""}},
    )
    task = asyncio.create_task(_drive_run(db, run_id))
    _active_tasks[run_id] = task
    task.add_done_callback(lambda t, rid=run_id: _active_tasks.pop(rid, None))
    return effective_run(await db[RUNS_COLLECTION].find_one({"_id": run_id}))


def effective_run(doc: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """What the console should believe about a run. A run an operator cancelled is cancelled, whatever status a stray
    worker on another server wrote back over it afterwards."""
    if doc and doc.get("cancelledAt") and doc.get("status") not in TERMINAL_STATES:
        return {**doc, "status": FSMState.HALTED.value, "haltReason": doc.get("haltReason") or "Cancelled by an operator."}
    return doc


async def get_run_status(db, run_id: str) -> Optional[dict[str, Any]]:
    return effective_run(await db[RUNS_COLLECTION].find_one({"_id": run_id}))


async def list_runs(db, limit: int = 25) -> list[dict[str, Any]]:
    cursor = db[RUNS_COLLECTION].find({}).sort("createdAt", -1).limit(limit)
    return [effective_run(d) for d in await cursor.to_list(length=limit)]


async def resume_pending_corridor_runs(db) -> int:
    """Called once at server startup: relaunches any run left in a
    non-terminal state by a previous process (crash, deploy, restart) from
    exactly the cycle/principal/state it last persisted. Returns how many
    were resumed."""
    resumed = 0
    # A run an operator cancelled stays halted, even if a stray worker wrote its old status back.
    await db[RUNS_COLLECTION].update_many({"cancelledAt": {"$exists": True}, "status": {"$nin": list(TERMINAL_STATES)}},
                                          {"$set": {"status": FSMState.HALTED.value}})
    cursor = db[RUNS_COLLECTION].find({"status": {"$nin": list(TERMINAL_STATES)}, "cancelledAt": {"$exists": False}})
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


def _build_fsm(db, ledger: ImmutableLedger, run_doc: dict[str, Any]) -> HFTCorridorFSM:
    config = dict(run_doc["config"])
    config["run_id"] = run_doc["_id"]
    # Real Motor database handle — mint_provider="comet" needs it to price
    # the IMC->USDT leg off the same treasury rate book retail swaps use
    # (routes.swap_engine.calculate_backend_rate), instead of Comet's IMM
    # (whose custodial wallet turned out not to recognize a plain on-chain
    # deposit — see state_engine._execute_mint_comet's docstring).
    config["db"] = db
    config["run_started_at"] = run_doc.get("createdAt")
    config["resume_cycle"] = run_doc["currentCycle"]
    config["resume_principal_usd"] = run_doc["currentUsdPrincipal"]
    config["resume_kes_float"] = run_doc.get("currentKesFloat", 0.0)
    config["resume_state"] = run_doc["status"]
    config["resume_cumulative_pnl_usd"] = run_doc.get("cumulativePnl", 0.0)
    config["resume_exit_path_chosen"] = run_doc.get("exitPathChosen")
    # AWAITING_MANUAL_MINT state (see state_engine._execute_mint_comet):
    # restores what a real on-chain balance poll is waiting to observe, so
    # a restart mid-wait resumes watching for the same mint rather than
    # losing track of it.
    config["resume_swap_report"] = run_doc.get("swap")
    config["resume_exit_swap_booked"] = bool(run_doc.get("exitSwapBooked"))
    config["resume_exit_report"] = run_doc.get("exit")
    config["resume_pending_mint_expected_imc"] = run_doc.get("pendingMintExpectedImc")
    config["resume_pending_mint_external_id"] = run_doc.get("pendingMintExternalId")
    config["resume_pending_mint_treasury_imc_before"] = run_doc.get("pendingMintTreasuryImcBefore")
    # AWAITING_MANUAL_TOPUP state (see state_engine._execute_procure) — same
    # resume-on-restart treatment as the manual-mint fields above.
    config["resume_pending_topup_expected_kes"] = run_doc.get("pendingTopupExpectedKes")
    config["resume_pending_topup_txn_id"] = run_doc.get("pendingTopupTxnId")
    config["resume_pending_topup_payout_balance_before"] = run_doc.get("pendingTopupPayoutBalanceBefore")
    return HFTCorridorFSM(ledger, starting_capital_usd=run_doc["startingUsd"], config=config)


async def _persist_tick(db, run_id: str, bot: HFTCorridorFSM) -> bool:
    update: dict[str, Any] = {
        "status": bot.state.value,
        "currentCycle": bot.current_cycle,
        "currentUsdPrincipal": bot.current_usd_principal,
        "currentKesFloat": bot.current_kes_float,
        "exitPathChosen": bot.exit_path_chosen,
        "cyclePnl": bot.last_cycle_pnl_usd,
        "cumulativePnl": bot.cumulative_pnl_usd,
        "pendingMintExpectedImc": bot.pending_mint_expected_imc,
        "pendingMintExternalId": bot.pending_mint_external_id,
        "pendingMintTreasuryImcBefore": bot.pending_mint_treasury_imc_before,
        "pendingTopupExpectedKes": bot.pending_topup_expected_kes,
        "pendingTopupTxnId": bot.pending_topup_txn_id,
        "pendingTopupPayoutBalanceBefore": bot.pending_topup_payout_balance_before,
        "updatedAt": datetime.utcnow(),
    }
    if bot.swap_report is not None:
        update["swap"] = bot.swap_report
    if bot.exit_report is not None:
        update["exit"] = bot.exit_report
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
    # Never write over a run an operator has cancelled: the cancel is recorded as `cancelledAt`, and a worker that is
    # still alive (it can lose a race with the cancel) must stop instead of putting its old status back.
    update["ownerId"], update["ownerHeartbeat"] = PROCESS_ID, datetime.utcnow()
    res = await db[RUNS_COLLECTION].update_one(
        {"_id": run_id, "cancelledAt": {"$exists": False}, "$or": [{"ownerId": PROCESS_ID}, {"ownerId": {"$exists": False}}]},
        {"$set": update})
    return getattr(res, "matched_count", 1) != 0


async def _claim_run(db, run_id: str) -> bool:
    """Takes (or keeps) the lease on a run. False when another server holds a fresh lease or the run was cancelled."""
    now = datetime.utcnow()
    res = await db[RUNS_COLLECTION].update_one(
        {"_id": run_id, "cancelledAt": {"$exists": False},
         "$or": [{"ownerId": {"$exists": False}}, {"ownerId": PROCESS_ID}, {"ownerHeartbeat": {"$lt": now - timedelta(seconds=LEASE_SECONDS)}}]},
        {"$set": {"ownerId": PROCESS_ID, "ownerHeartbeat": now}})
    return getattr(res, "matched_count", 1) != 0


async def _is_cancelled(db, run_id: str) -> bool:
    doc = await db[RUNS_COLLECTION].find_one({"_id": run_id}, {"cancelledAt": 1})
    return bool(doc and doc.get("cancelledAt"))


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

    # One server per run. If another server holds a fresh lease, wait: take over only if it stops (crash, shutdown).
    warned = False
    while not await _claim_run(db, run_id):
        current = await db[RUNS_COLLECTION].find_one({"_id": run_id}, {"status": 1, "cancelledAt": 1})
        if not current or current.get("cancelledAt") or current.get("status") in TERMINAL_STATES:
            return
        if not warned:
            logger.warning("Corridor run %s is being driven by another server; this one will only take over if it stops", run_id)
            warned = True
        await asyncio.sleep(LEASE_RETRY_SECONDS)
    run_doc = await db[RUNS_COLLECTION].find_one({"_id": run_id}) or run_doc

    ledger = ImmutableLedger(db_collection=db["transactions"])

    try:
        bot = _build_fsm(db, ledger, run_doc)
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
            if await _is_cancelled(db, run_id):          # checked before every step, so a cancelled run never takes another
                logger.info("Corridor run %s was cancelled; its worker is stopping", run_id)
                return
            await bot.tick()
            if not await _persist_tick(db, run_id, bot):
                logger.info("Corridor run %s was cancelled during a step; its worker is stopping", run_id)
                return
    except Exception as e:
        traceback.print_exc()
        logger.error("Corridor run %s crashed mid-tick: %s", run_id, e)
        await db[RUNS_COLLECTION].update_one(
            {"_id": run_id, "cancelledAt": {"$exists": False}, "ownerId": PROCESS_ID},
            {"$set": {"status": FSMState.HALTED.value, "haltReason": str(e), "updatedAt": datetime.utcnow()}},
        )
        return

    logger.info("Corridor run %s finished: %s (cycle %s, $%.4f)",
                run_id, bot.state.value, bot.current_cycle, bot.current_usd_principal)
