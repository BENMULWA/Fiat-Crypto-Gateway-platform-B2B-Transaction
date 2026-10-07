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
import types

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

    @classmethod
    def _matches(cls, doc, query):
        """The parts of a Mongo filter these tests use: equality, $exists, $lt, $nin and $or."""
        for key, cond in query.items():
            if key == "$or":
                if not any(cls._matches(doc, sub) for sub in cond):
                    return False
            elif isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
                have = doc.get(key)
                if "$exists" in cond and (key in doc) != bool(cond["$exists"]):
                    return False
                if "$nin" in cond and have in set(cond["$nin"]):
                    return False
                if "$lt" in cond and not (key in doc and have is not None and have < cond["$lt"]):
                    return False
            elif doc.get(key) != cond:
                return False
        return True

    @staticmethod
    def _apply(doc, update):
        doc.update(update.get("$set", {}))
        for k in update.get("$unset", {}):
            doc.pop(k, None)

    async def update_one(self, query, update):
        doc = self.docs.get(query.get("_id"))
        if doc is None or not self._matches(doc, query):
            return {"matched_count": 0}
        self._apply(doc, update)
        return {"matched_count": 1, "modified_count": 1}

    async def update_many(self, query, update):
        hit = [d for d in self.docs.values() if self._matches(d, query)]
        for d in hit:
            self._apply(d, update)
        return {"matched_count": len(hit), "modified_count": len(hit)}

    def find(self, query=None):
        query = query or {}
        return FakeCursor([d for d in self.docs.values() if self._matches(d, query)])

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


def _make_fake_impala_provider(discount=0.06):
    """Same shared-state shape as Brain_Engine/simulate.py's fake — PROCURE
    now tops up via a real STK push (services.impala_airtime.topup_via_stk)
    and polls the real payout balance for the confirmed delta, so tests
    need both fakes wired together, not just a send_airtime stand-in."""
    state = {"balance": 10_000_000.0}

    def fake_get_payout_balance():
        return {"artm_balance": state["balance"], "currency": "KES", "raw": {}}

    def fake_topup_via_stk(amount_kes, paying_phone_number):
        state["balance"] += amount_kes * (1 + discount)
        return {"status": "success"}

    return fake_get_payout_balance, fake_topup_via_stk


_fake_get_payout_balance, _fake_topup_via_stk = _make_fake_impala_provider()


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
            "get_payout_balance_fn": _fake_get_payout_balance,
            "topup_via_stk_fn": _fake_topup_via_stk,
            "stk_paying_phone": "0700000000",
            "stk_poll_interval_seconds": 0.01,
            "stk_poll_attempts": 3,
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
    # A rejected/unconfirmed STK top-up no longer halts the run outright —
    # it pauses into AWAITING_MANUAL_TOPUP and waits for a real balance
    # increase (see state_engine._execute_procure). To exercise a genuine,
    # unrecoverable halt here, use a payout-balance read failure instead:
    # PROCURE refuses to top up blind if it can't even read the starting
    # balance, regardless of any real PROCUREMENT_WALLET_N2 env value.
    def failing_get_payout_balance():
        raise RuntimeError("simulated payout balance outage")

    db = FakeDB()
    run_doc = _fresh_run_doc(run_id="RUN-TEST-HALT")
    run_doc["config"]["get_payout_balance_fn"] = failing_get_payout_balance
    db[corridor_worker.RUNS_COLLECTION].docs[run_doc["_id"]] = run_doc

    asyncio.run(corridor_worker._drive_run(db, run_doc["_id"]))

    final = db[corridor_worker.RUNS_COLLECTION].docs[run_doc["_id"]]
    assert final["status"] == FSMState.HALTED.value
    assert "simulated payout balance outage" in final["haltReason"]


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


# --- single-cycle runs and the linear reseller markup ---------------------

def test_drive_run_with_one_cycle_goes_straight_to_exit_without_a_rollover_hold():
    """cycles=1 is a single pass: procure -> liquidate -> mint -> exit. The
    FSM must not park in AWAITING_OPPORTUNITY or fire a second STK push."""
    db = FakeDB()
    run_doc = _fresh_run_doc(cycles=1)
    db[corridor_worker.RUNS_COLLECTION].docs[run_doc["_id"]] = run_doc

    seen = []
    original = db[corridor_worker.RUNS_COLLECTION].update_one

    async def spy(query, update):
        result = await original(query, update)
        if "$set" in update and "status" in update["$set"]:
            seen.append(update["$set"]["status"])
        return result

    db[corridor_worker.RUNS_COLLECTION].update_one = spy
    asyncio.run(corridor_worker._drive_run(db, run_doc["_id"]))

    final = db[corridor_worker.RUNS_COLLECTION].docs[run_doc["_id"]]
    assert final["status"] == FSMState.COMPLETED.value
    assert final["currentCycle"] == 1
    assert FSMState.AWAITING_OPPORTUNITY.value not in seen
    assert final["finalUsd"] > final["startingUsd"]


@pytest.mark.parametrize("bad_cycles", [0, -1, 11, True, 2.5])
def test_start_corridor_run_rejects_invalid_cycles(bad_cycles):
    db = FakeDB()
    with pytest.raises(ValueError, match="Cycles must be"):
        asyncio.run(corridor_worker.start_corridor_run(db, corridor_id="airtel_5x", amount_usd=1.0, cycles=bad_cycles))
    assert db[corridor_worker.RUNS_COLLECTION].docs == {}  # nothing was written


def test_start_corridor_run_records_the_requested_cycles_in_the_run_config(monkeypatch):
    """The persisted config is what _build_fsm reads on start and on a restart
    resume, so the cycle count must survive in the run document. _drive_run is
    stubbed so no real STK push or Comet call can happen in a test."""
    async def _noop(db, run_id):
        return None

    monkeypatch.setattr(corridor_worker, "_drive_run", _noop)
    db = FakeDB()

    async def go():
        doc = await corridor_worker.start_corridor_run(db, corridor_id="airtel_5x", amount_usd=1.0, cycles=1)
        await asyncio.sleep(0)
        return doc

    doc = asyncio.run(go())
    assert doc["config"]["cycles"] == 1
    assert db[corridor_worker.RUNS_COLLECTION].docs[doc["_id"]]["config"]["cycles"] == 1


def test_start_corridor_run_defaults_to_five_cycles(monkeypatch):
    async def _noop(db, run_id):
        return None

    monkeypatch.setattr(corridor_worker, "_drive_run", _noop)
    db = FakeDB()

    async def go():
        return await corridor_worker.start_corridor_run(db, corridor_id="airtel_5x", amount_usd=1.0)

    assert asyncio.run(go())["config"]["cycles"] == 5


def test_projected_yield_uses_the_linear_reseller_markup():
    """Live evidence: paying 500 KES returned 525 KES of float (x1.05), not
    500 / (1 - 0.05) = 526.3. One cycle at a 5% discount is exactly 1.05."""
    from Brain_Engine.Discovery_Engine import IMMDiscoveryEngine

    engine = IMMDiscoveryEngine()
    one = engine.project_corridor_yield(discount_rate=0.05, fx_edge_pct=0.0, cycles=1)
    assert one["single_cycle_multiplier"] == pytest.approx(1.05)
    assert one["projected_profit_pct"] == pytest.approx(5.0)

    five = engine.project_corridor_yield(discount_rate=0.05, fx_edge_pct=0.0, cycles=5)
    assert five["total_5x_multiplier"] == pytest.approx(1.05 ** 5, abs=1e-4)
    assert five["skim_mode_multiplier"] == pytest.approx(1 + 5 * 0.05)


# --- cancelling a run ------------------------------------------------------

def _stuck_waiting_for_topup_doc(run_id="RUN-STUCK-1"):
    doc = _fresh_run_doc(run_id=run_id, status="AWAITING_MANUAL_TOPUP")
    # The fake provider balance never rises above this, so the run waits forever.
    doc.update(pendingTopupExpectedKes=200.0, pendingTopupPayoutBalanceBefore=10_000_000.0, pendingTopupTxnId="B2B-X-C1")
    return doc


def test_cancel_stops_a_running_task_and_marks_the_run_halted():
    db = FakeDB()
    doc = _stuck_waiting_for_topup_doc()
    db[corridor_worker.RUNS_COLLECTION].docs[doc["_id"]] = doc

    async def go():
        task = asyncio.create_task(corridor_worker._drive_run(db, doc["_id"]))
        corridor_worker._active_tasks[doc["_id"]] = task
        await asyncio.sleep(0.1)
        assert not task.done()  # it really is stuck waiting
        result = await corridor_worker.cancel_corridor_run(db, doc["_id"], cancelled_by="admin-1")
        corridor_worker._active_tasks.pop(doc["_id"], None)
        return task, result

    task, result = asyncio.run(go())
    assert task.cancelled()
    assert result["status"] == FSMState.HALTED.value
    assert "Cancelled by an operator" in result["haltReason"] and "AWAITING_MANUAL_TOPUP" in result["haltReason"]
    assert result["cancelledBy"] == "admin-1"
    assert result["pendingTopupExpectedKes"] is None


def test_a_cancelled_run_is_not_resumed_after_a_restart():
    db = FakeDB()
    doc = _stuck_waiting_for_topup_doc()
    db[corridor_worker.RUNS_COLLECTION].docs[doc["_id"]] = doc
    asyncio.run(corridor_worker.cancel_corridor_run(db, doc["_id"]))  # no live task in this process
    assert asyncio.run(corridor_worker.resume_pending_corridor_runs(db)) == 0


def test_cancel_unknown_and_finished_runs_are_rejected():
    db = FakeDB()
    with pytest.raises(LookupError):
        asyncio.run(corridor_worker.cancel_corridor_run(db, "RUN-NOPE"))
    done = _fresh_run_doc(run_id="RUN-DONE", status="COMPLETED")
    db[corridor_worker.RUNS_COLLECTION].docs["RUN-DONE"] = done
    with pytest.raises(ValueError, match="already finished"):
        asyncio.run(corridor_worker.cancel_corridor_run(db, "RUN-DONE"))


# --- procure from the float already held (no STK push) ---------------------

def _existing_float_doc(balance, run_id="RUN-FLOAT-1"):
    """A one-cycle run that must not send any STK push, with a fake float of `balance` KES."""
    stk_calls = []

    def _no_stk(amount_kes, paying_phone_number):
        stk_calls.append((amount_kes, paying_phone_number))
        raise AssertionError("existing_float mode must never send an STK push")

    doc = _fresh_run_doc(run_id=run_id, cycles=1)
    doc["config"].update(
        procure_mode="existing_float",
        topup_via_stk_fn=_no_stk,
        stk_paying_phone=None,  # no phone is needed in this mode
        get_payout_balance_fn=lambda: {"artm_balance": balance, "currency": "KES", "raw": {}},
    )
    return doc, stk_calls


def test_existing_float_mode_completes_without_any_stk_push_or_phone():
    db = FakeDB()
    doc, stk_calls = _existing_float_doc(balance=10_000.0)
    db[corridor_worker.RUNS_COLLECTION].docs[doc["_id"]] = doc
    asyncio.run(corridor_worker._drive_run(db, doc["_id"]))

    final = db[corridor_worker.RUNS_COLLECTION].docs[doc["_id"]]
    assert stk_calls == []
    assert final["status"] == FSMState.COMPLETED.value
    assert final["finalUsd"] > final["startingUsd"]


def test_existing_float_mode_allocates_amount_times_one_plus_discount():
    db = FakeDB()
    doc, _ = _existing_float_doc(balance=10_000.0)
    db[corridor_worker.RUNS_COLLECTION].docs[doc["_id"]] = doc
    asyncio.run(corridor_worker._drive_run(db, doc["_id"]))
    # The ledger leg that credits the airtime node carries the allocated airtime value.
    credits = [e for e in db["transactions"].docs.values()
               if e.get("txn_type") == "PROCURE" and e.get("to_node") == "N2"]
    assert len(credits) == 1
    assert credits[0]["amount"] == pytest.approx(doc["startingUsd"] * 129.50 * (1 + 0.06))
    assert str(credits[0]["external_ref"]).startswith("FLOAT-")


def test_existing_float_mode_halts_when_the_float_is_too_small():
    db = FakeDB()
    doc, stk_calls = _existing_float_doc(balance=1.0)
    db[corridor_worker.RUNS_COLLECTION].docs[doc["_id"]] = doc
    asyncio.run(corridor_worker._drive_run(db, doc["_id"]))
    final = db[corridor_worker.RUNS_COLLECTION].docs[doc["_id"]]
    assert final["status"] == FSMState.HALTED.value
    assert "Not enough free airtime float" in final["haltReason"]
    assert stk_calls == []


def test_existing_float_mode_fails_closed_when_the_float_cannot_be_read():
    db = FakeDB()
    doc, _ = _existing_float_doc(balance=10_000.0)

    def boom():
        raise RuntimeError("provider down")

    doc["config"]["get_payout_balance_fn"] = boom
    db[corridor_worker.RUNS_COLLECTION].docs[doc["_id"]] = doc
    asyncio.run(corridor_worker._drive_run(db, doc["_id"]))
    final = db[corridor_worker.RUNS_COLLECTION].docs[doc["_id"]]
    assert final["status"] == FSMState.HALTED.value and "refusing to allocate it blind" in final["haltReason"]


def test_start_corridor_run_records_and_validates_procure_mode(monkeypatch):
    async def _noop(db, run_id):
        return None

    monkeypatch.setattr(corridor_worker, "_drive_run", _noop)

    async def go(mode):
        return await corridor_worker.start_corridor_run(FakeDB(), corridor_id="airtel_5x", amount_usd=1.0, cycles=1, procure_mode=mode)

    assert asyncio.run(go("existing_float"))["config"]["procure_mode"] == "existing_float"
    assert asyncio.run(go("stk"))["config"]["procure_mode"] == "stk"
    with pytest.raises(ValueError, match="procure_mode"):
        asyncio.run(go("magic"))


def test_start_corridor_run_records_and_validates_swap_mode(monkeypatch):
    async def _noop(db, run_id):
        return None

    monkeypatch.setattr(corridor_worker, "_drive_run", _noop)

    async def go(mode):
        return await corridor_worker.start_corridor_run(FakeDB(), corridor_id="airtel_5x", amount_usd=1.0, cycles=1, swap_mode=mode)

    assert asyncio.run(go("pool"))["config"]["swap_mode"] == "pool"
    assert asyncio.run(go("reserve"))["config"]["swap_mode"] == "reserve"
    with pytest.raises(ValueError, match="swap_mode"):
        asyncio.run(go("magic"))


@pytest.mark.parametrize("bad", [0, 0.0005, 0.51, 1, True, "0.02"])
def test_start_corridor_run_rejects_an_unreasonable_pool_slippage_limit(bad, monkeypatch):
    async def _noop(db, run_id):
        return None

    monkeypatch.setattr(corridor_worker, "_drive_run", _noop)
    with pytest.raises(ValueError, match="pool_max_slippage"):
        asyncio.run(corridor_worker.start_corridor_run(FakeDB(), corridor_id="airtel_5x", amount_usd=1.0, cycles=1, swap_mode="pool", pool_max_slippage=bad))


def test_start_corridor_run_records_the_pool_slippage_limit(monkeypatch):
    async def _noop(db, run_id):
        return None

    monkeypatch.setattr(corridor_worker, "_drive_run", _noop)
    doc = asyncio.run(corridor_worker.start_corridor_run(FakeDB(), corridor_id="airtel_5x", amount_usd=1.0, cycles=1, swap_mode="pool", pool_max_slippage=0.05))
    assert doc["config"]["pool_max_slippage"] == 0.05 and doc["config"]["swap_mode"] == "pool"


def test_a_deliberate_test_run_may_accept_up_to_fifty_percent_slippage(monkeypatch):
    async def _noop(db, run_id):
        return None

    monkeypatch.setattr(corridor_worker, "_drive_run", _noop)
    doc = asyncio.run(corridor_worker.start_corridor_run(FakeDB(), corridor_id="airtel_5x", amount_usd=1.0, cycles=1, swap_mode="pool", pool_max_slippage=0.30))
    assert doc["config"]["pool_max_slippage"] == 0.30



# ------------------------------------------------------------------ a cancel must stick
class _Res(dict):
    """The fake's update result: a dict, but with the attribute Motor's UpdateResult has."""
    matched_count = property(lambda self: self["matched_count"])


def _wrap_results(col):
    for name in ("update_one", "update_many"):
        orig = getattr(col, name)

        async def wrapped(*a, _orig=orig, **k):
            return _Res(await _orig(*a, **k))

        setattr(col, name, wrapped)
    return col


def _db_with_run(status="AWAITING_MANUAL_MINT"):
    db = FakeDB()
    col = _wrap_results(db["corridor_runs"])
    col.docs["RUN-C"] = {"_id": "RUN-C", "status": status, "currentCycle": 1, "currentUsdPrincipal": 0.0386, "startingUsd": 0.0386,
                         "currentKesFloat": 0.0, "cumulativePnl": 0.0, "config": {"swap_mode": "pool"}}
    return db, col


def test_a_cancel_is_recorded_before_anything_else_and_survives_a_late_write_from_a_stray_worker():
    """The 2026-10-07 bug: the cancel set HALTED, then a worker that was still alive wrote AWAITING_MANUAL_MINT back."""
    db, col = _db_with_run()

    async def go():
        doc = await corridor_worker.cancel_corridor_run(db, "RUN-C", cancelled_by="admin-1")
        # a stray worker now tries to persist its old state, exactly what overwrote the cancel before
        stray = types.SimpleNamespace(state=corridor_worker.FSMState.AWAITING_MANUAL_MINT, current_cycle=1, current_usd_principal=0.0386,
                                      current_kes_float=0.0, exit_path_chosen=None, last_cycle_pnl_usd=0.0, cumulative_pnl_usd=0.0,
                                      pending_mint_expected_imc=0.04, pending_mint_external_id=None, pending_mint_treasury_imc_before=None,
                                      pending_topup_expected_kes=None, pending_topup_txn_id=None, pending_topup_payout_balance_before=None,
                                      swap_report=None, exit_report=None, halt_reason=None)
        wrote = await corridor_worker._persist_tick(db, "RUN-C", stray)
        return doc, wrote

    doc, wrote = asyncio.run(go())
    assert wrote is False                                                      # the stray write was refused
    assert doc["status"] == "HALTED" and col.docs["RUN-C"]["status"] == "HALTED" and col.docs["RUN-C"]["cancelledBy"] == "admin-1"


def test_a_worker_for_a_cancelled_run_stops_before_taking_another_step(monkeypatch):
    db, col = _db_with_run()
    col.docs["RUN-C"]["cancelledAt"] = "now"                                   # cancelled while this worker was still alive
    ticks = []

    class Bot:
        state = corridor_worker.FSMState.AWAITING_MANUAL_MINT

        async def tick(self):
            ticks.append(1)

    monkeypatch.setattr(corridor_worker, "_build_fsm", lambda db_, ledger, doc: Bot())
    asyncio.run(corridor_worker._drive_run(db, "RUN-C"))
    assert ticks == []                                                         # not one more step on a cancelled run


def test_startup_heals_a_cancelled_run_whose_status_was_overwritten_and_does_not_resume_it(monkeypatch):
    db, col = _db_with_run(status="AWAITING_MANUAL_MINT")
    col.docs["RUN-C"]["cancelledAt"] = "earlier"                               # cancelled, but a stray worker put the old status back
    started = []

    async def fake_drive(db_, run_id):
        started.append(run_id)

    monkeypatch.setattr(corridor_worker, "_drive_run", fake_drive)
    resumed = asyncio.run(corridor_worker.resume_pending_corridor_runs(db))
    assert resumed == 0 and started == []
    assert col.docs["RUN-C"]["status"] == "HALTED"                             # healed, so the console stops showing it as open


def test_an_uncancelled_open_run_is_still_resumed_after_a_restart(monkeypatch):
    db, col = _db_with_run()
    started = []

    async def fake_drive(db_, run_id):
        started.append(run_id)

    monkeypatch.setattr(corridor_worker, "_drive_run", fake_drive)

    async def go():
        n = await corridor_worker.resume_pending_corridor_runs(db)
        await asyncio.sleep(0)
        return n

    assert asyncio.run(go()) == 1 and started == ["RUN-C"]



# ------------------------------------------------------------------ only one server drives a run
def test_a_second_server_cannot_take_a_run_another_server_is_driving(monkeypatch):
    db, col = _db_with_run()

    async def go():
        monkeypatch.setattr(corridor_worker, "PROCESS_ID", "server-A")
        a1 = await corridor_worker._claim_run(db, "RUN-C")           # A takes it
        a2 = await corridor_worker._claim_run(db, "RUN-C")           # A re-claiming its own run is fine
        monkeypatch.setattr(corridor_worker, "PROCESS_ID", "server-B")
        b = await corridor_worker._claim_run(db, "RUN-C")            # B is refused while A's lease is fresh
        return a1, a2, b

    a1, a2, b = asyncio.run(go())
    assert (a1, a2, b) == (True, True, False)
    assert col.docs["RUN-C"]["ownerId"] == "server-A"


def test_a_server_can_take_over_once_the_owners_lease_has_gone_stale(monkeypatch):
    db, col = _db_with_run()
    from datetime import datetime, timedelta
    col.docs["RUN-C"].update(ownerId="server-A", ownerHeartbeat=datetime.utcnow() - timedelta(seconds=corridor_worker.LEASE_SECONDS + 5))
    monkeypatch.setattr(corridor_worker, "PROCESS_ID", "server-B")
    assert asyncio.run(corridor_worker._claim_run(db, "RUN-C")) is True            # A stopped heartbeating: crash or shutdown
    assert col.docs["RUN-C"]["ownerId"] == "server-B"


def test_a_cancelled_run_cannot_be_claimed(monkeypatch):
    db, col = _db_with_run()
    col.docs["RUN-C"]["cancelledAt"] = "now"
    assert asyncio.run(corridor_worker._claim_run(db, "RUN-C")) is False


def test_a_server_that_lost_the_run_stops_writing_to_it(monkeypatch):
    """Server B took over after A went quiet. If A wakes up and persists, it must be refused, not overwrite B."""
    db, col = _db_with_run()
    from datetime import datetime, timedelta
    col.docs["RUN-C"].update(ownerId="server-B", ownerHeartbeat=datetime.utcnow())
    bot = types.SimpleNamespace(state=corridor_worker.FSMState.AWAITING_MANUAL_MINT, current_cycle=1, current_usd_principal=0.0386,
                                current_kes_float=0.0, exit_path_chosen=None, last_cycle_pnl_usd=0.0, cumulative_pnl_usd=0.0,
                                pending_mint_expected_imc=0.04, pending_mint_external_id=None, pending_mint_treasury_imc_before=None,
                                pending_topup_expected_kes=None, pending_topup_txn_id=None, pending_topup_payout_balance_before=None,
                                swap_report=None, exit_report=None, halt_reason=None)
    monkeypatch.setattr(corridor_worker, "PROCESS_ID", "server-A")
    assert asyncio.run(corridor_worker._persist_tick(db, "RUN-C", bot)) is False
    assert col.docs["RUN-C"]["ownerId"] == "server-B" and "swap" not in col.docs["RUN-C"]


def test_a_worker_that_cannot_claim_waits_and_never_ticks(monkeypatch):
    """A second server's worker must not take a single step while another server owns the run."""
    db, col = _db_with_run()
    from datetime import datetime
    col.docs["RUN-C"].update(ownerId="server-A", ownerHeartbeat=datetime.utcnow())
    monkeypatch.setattr(corridor_worker, "PROCESS_ID", "server-B")
    monkeypatch.setattr(corridor_worker, "LEASE_RETRY_SECONDS", 0.01)
    ticks = []

    class Bot:
        state = corridor_worker.FSMState.AWAITING_MANUAL_MINT

        async def tick(self):
            ticks.append(1)

    monkeypatch.setattr(corridor_worker, "_build_fsm", lambda db_, ledger, doc: Bot())

    async def go():
        task = asyncio.create_task(corridor_worker._drive_run(db, "RUN-C"))
        await asyncio.sleep(0.1)                                                   # waits, retrying the claim
        assert not task.done() and ticks == []
        col.docs["RUN-C"]["cancelledAt"] = "now"                                   # the run is cancelled meanwhile: the worker gives up
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(go())
    assert ticks == []



# ------------------------------------------------------------------ a cancelled run reads as cancelled
def test_the_console_sees_a_cancelled_run_as_halted_even_if_a_stray_worker_rewrote_its_status():
    db, col = _db_with_run(status="AWAITING_MANUAL_MINT")
    col.docs["RUN-C"]["cancelledAt"] = "earlier"                               # cancelled, then a stray worker put the old status back

    async def go():
        one = await corridor_worker.get_run_status(db, "RUN-C")
        many = await corridor_worker.list_runs(db)
        return one, many

    one, many = asyncio.run(go())
    assert one["status"] == "HALTED" and many[0]["status"] == "HALTED"
    assert col.docs["RUN-C"]["status"] == "AWAITING_MANUAL_MINT"               # the stored value is untouched; only what is reported changes


def test_an_uncancelled_open_run_still_reads_as_open():
    db, col = _db_with_run(status="AWAITING_MANUAL_MINT")
    assert asyncio.run(corridor_worker.get_run_status(db, "RUN-C"))["status"] == "AWAITING_MANUAL_MINT"


def test_a_finished_run_keeps_its_own_status_even_if_it_was_also_cancelled():
    db, col = _db_with_run(status="COMPLETED")
    col.docs["RUN-C"]["cancelledAt"] = "x"
    assert asyncio.run(corridor_worker.get_run_status(db, "RUN-C"))["status"] == "COMPLETED"
