"""Tests for workers/corridor_worker.py — the background-task runner that
replaced the old blocking /corridor/execute-hft endpoint. Covers:
 - a full run persisting state after every tick (not just on completion)
 - the resume sweep picking up non-terminal runs and skipping terminal /
   already-active ones
 - start_corridor_run's eligibility validation, which must reject before
   touching any external dependency

External dependencies are injected via HFTCorridorFSM's config (same
mechanism Brain_Engine/simulate.py uses) directly in the fake run_doc's
"config" — legitimate here because these are in-memory Python test
doubles, not real Mongo documents (a real BSON document can't hold a
function, which is exactly why a real /corridor/start caller can never do
this — every real run always uses the real singletons)."""
import asyncio

import pytest

import workers.corridor_worker as corridor_worker
from Brain_Engine.state_engine import FSMState


class FakeCollection:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    async def insert_one(self, document):
        # Also used as the fake "transactions" collection by the ledger,
        # whose entries have no _id of their own — auto-assign one, same
        # as a real Mongo insert would.
        doc_id = document.get("_id", object())
        document = {**document, "_id": doc_id}
        self.docs[doc_id] = document
        return {"inserted_id": doc_id}

    async def find_one(self, query, projection=None):
        doc = self.docs.get(query.get("_id"))
        if doc is None:
            return None
        if projection:
            return {k: doc.get(k) for k in projection if projection[k]}
        return dict(doc)

    async def update_one(self, query, update):
        doc = self.docs.get(query.get("_id"))
        if doc is None:
            return {"matched_count": 0}
        if "$set" in update:
            doc.update(update["$set"])
        return {"matched_count": 1, "modified_count": 1}

    def find(self, query=None):
        query = query or {}
        matches = list(self.docs.values())
        if "status" in query and "$nin" in query["status"]:
            excluded = set(query["status"]["$nin"])
            matches = [d for d in matches if d.get("status") not in excluded]
        return FakeCursor(matches)

    def aggregate(self, pipeline):
        # Used by ImmutableLedger's risk-check helpers (get_node_volume_today,
        # get_vault_backed_total, get_balance) against the fake "transactions"
        # collection. Always "no prior volume" is sufficient for these tests —
        # none of them depend on real cross-run aggregation.
        return FakeCursor([])


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    async def to_list(self, length=None):
        return list(self._docs)

    def __aiter__(self):
        return iter(self._docs).__iter__() if False else _AIter(self._docs)


class _AIter:
    def __init__(self, docs):
        self._it = iter(docs)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class FakeDB:
    def __init__(self):
        self._collections = {}

    def __getitem__(self, name):
        return self._collections.setdefault(name, FakeCollection())


def _fake_send_airtime(phone, amount_kes, reference):
    return {"status": "success", "receipt_id": f"TEST-{reference}"}


def _fake_get_merchant_balance():
    return {"status": "success", "data": {"kesBalance": 10_000_000.0}}


def _fake_get_vault_balance():
    return 10_000_000.0


async def _fake_celo_swap(usda_amount):
    return "0x" + "ab" * 32


def _fresh_run_doc(run_id="RUN-TEST-1", status="IDLE", cycle=1, principal=100 / 129.50, cycles=5, find_opportunity_fn=None):
    return {
        "_id": run_id,
        "corridorId": "airtel_5x",
        "config": {
            "cycles": cycles,
            "discount": 0.06,
            "fx_edge": 0.0,
            "node_procure": "N2",
            "node_liquidate": "N5",
            "send_airtime_fn": _fake_send_airtime,
            "get_merchant_balance_fn": _fake_get_merchant_balance,
            "get_vault_balance_fn": _fake_get_vault_balance,
            "celo_swap_fn": _fake_celo_swap,
            "find_opportunity_fn": find_opportunity_fn or (lambda: {
                "id": "airtel_5x", "node_procure": "N2", "node_liquidate": "N5", "discount": 0.06, "fx_edge": 0.0,
            }),
            "poll_seconds": 0.01,
        },
        "status": status,
        "currentCycle": cycle,
        "currentUsdPrincipal": principal,
        "currentKesFloat": 0.0,
        "startingUsd": principal,
        "finalUsd": None,
        "profit": None,
        "haltReason": None,
    }


def test_drive_run_completes_5_cycles_and_persists_final_state():
    db = FakeDB()
    run_doc = _fresh_run_doc()
    db[corridor_worker.RUNS_COLLECTION].docs[run_doc["_id"]] = run_doc

    asyncio.run(corridor_worker._drive_run(db, run_doc["_id"]))

    final = db[corridor_worker.RUNS_COLLECTION].docs[run_doc["_id"]]
    assert final["status"] == FSMState.COMPLETED.value
    assert final["currentCycle"] == 5
    assert final["finalUsd"] > final["startingUsd"]
    assert final["profit"] == pytest.approx(final["finalUsd"] - final["startingUsd"])


def test_drive_run_persists_every_intermediate_tick_not_just_the_final_one():
    """The whole point of moving off the blocking endpoint: state must be
    durable after every tick, not just when the run finishes — otherwise a
    restart mid-run loses just as much as the old design did."""
    db = FakeDB()
    run_doc = _fresh_run_doc()
    db[corridor_worker.RUNS_COLLECTION].docs[run_doc["_id"]] = run_doc

    seen_statuses = []
    original_update_one = db[corridor_worker.RUNS_COLLECTION].update_one

    async def spying_update_one(query, update):
        result = await original_update_one(query, update)
        if "$set" in update and "status" in update["$set"]:
            seen_statuses.append(update["$set"]["status"])
        return result

    db[corridor_worker.RUNS_COLLECTION].update_one = spying_update_one

    asyncio.run(corridor_worker._drive_run(db, run_doc["_id"]))

    # Every state PROCURE goes through should have been persisted at least
    # once — not just the terminal COMPLETED write.
    for expected in ("PROCURE", "LIQUIDATE", "MINT"):
        assert expected in seen_statuses, f"{expected} was never persisted mid-run"
    assert seen_statuses[-1] == FSMState.COMPLETED.value


def test_drive_run_halts_cleanly_and_records_halt_reason():
    def no_opportunity():
        return None  # AWAITING_OPPORTUNITY will hold forever with nothing eligible — force a halt another way instead

    def rejecting_send_airtime(phone, amount_kes, reference):
        raise RuntimeError("simulated provider rejection")

    db = FakeDB()
    run_doc = _fresh_run_doc(run_id="RUN-TEST-HALT")
    run_doc["config"]["send_airtime_fn"] = rejecting_send_airtime
    db[corridor_worker.RUNS_COLLECTION].docs[run_doc["_id"]] = run_doc

    asyncio.run(corridor_worker._drive_run(db, run_doc["_id"]))

    final = db[corridor_worker.RUNS_COLLECTION].docs[run_doc["_id"]]
    assert final["status"] == FSMState.HALTED.value
    assert "simulated provider rejection" in final["haltReason"]


def test_resume_pending_corridor_runs_relaunches_non_terminal_and_skips_terminal():
    async def scenario():
        db = FakeDB()
        pending = _fresh_run_doc(run_id="RUN-PENDING", status="AWAITING_OPPORTUNITY", cycle=3, principal=0.5)
        completed = _fresh_run_doc(run_id="RUN-DONE", status="COMPLETED", cycle=5, principal=0.9)
        db[corridor_worker.RUNS_COLLECTION].docs[pending["_id"]] = pending
        db[corridor_worker.RUNS_COLLECTION].docs[completed["_id"]] = completed

        resumed_count = await corridor_worker.resume_pending_corridor_runs(db)
        assert resumed_count == 1

        task = corridor_worker._active_tasks.get("RUN-PENDING")
        assert task is not None
        await task  # let the resumed run actually finish, in the same loop that scheduled it

        final_pending = db[corridor_worker.RUNS_COLLECTION].docs["RUN-PENDING"]
        assert final_pending["status"] == FSMState.COMPLETED.value
        assert final_pending["currentCycle"] == 5

        # The already-completed run must never have been touched by the sweep.
        assert db[corridor_worker.RUNS_COLLECTION].docs["RUN-DONE"]["currentCycle"] == 5
        assert "RUN-DONE" not in corridor_worker._active_tasks

    asyncio.run(scenario())


def test_resume_pending_corridor_runs_skips_a_run_already_active():
    async def scenario():
        db = FakeDB()
        run_doc = _fresh_run_doc(run_id="RUN-ALREADY-ACTIVE", status="PROCURE")
        db[corridor_worker.RUNS_COLLECTION].docs[run_doc["_id"]] = run_doc

        async def never_finishes():
            await asyncio.sleep(10)

        fake_task = asyncio.create_task(never_finishes())
        corridor_worker._active_tasks["RUN-ALREADY-ACTIVE"] = fake_task
        try:
            resumed_count = await corridor_worker.resume_pending_corridor_runs(db)
            assert resumed_count == 0
        finally:
            fake_task.cancel()
            corridor_worker._active_tasks.pop("RUN-ALREADY-ACTIVE", None)

    asyncio.run(scenario())


def test_start_corridor_run_rejects_unknown_corridor():
    db = FakeDB()
    with pytest.raises(ValueError, match="Unknown corridor"):
        asyncio.run(corridor_worker.start_corridor_run(db, corridor_id="not_a_real_corridor", amount_usd=1.0))
    assert db[corridor_worker.RUNS_COLLECTION].docs == {}


def test_start_corridor_run_rejects_ineligible_corridor_without_touching_dependencies():
    """telkom_5x's nodes (N1, N4) are both live=False in node_registry.py
    today — this must fail validation before ever constructing an FSM or
    calling any external dependency."""
    db = FakeDB()
    with pytest.raises(ValueError, match="disabled or one of its nodes"):
        asyncio.run(corridor_worker.start_corridor_run(db, corridor_id="telkom_5x", amount_usd=1.0))
    assert db[corridor_worker.RUNS_COLLECTION].docs == {}


def test_start_corridor_run_rejects_non_positive_amount():
    db = FakeDB()
    with pytest.raises(ValueError, match="greater than zero"):
        asyncio.run(corridor_worker.start_corridor_run(db, corridor_id="airtel_5x", amount_usd=0))
    assert db[corridor_worker.RUNS_COLLECTION].docs == {}
