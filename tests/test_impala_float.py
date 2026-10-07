"""Standalone ImpalaPay float top-up (services/impala_float.py).

The provider and the database are in-memory doubles: no STK push, no network."""
import asyncio
from datetime import datetime, timedelta

import pytest

import services.impala_float as svc
from services.impala_airtime import ImpalaAirtimeClient


class Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, key, direction):
        self.docs = sorted(self.docs, key=lambda d: d.get(key), reverse=direction < 0)
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, length=None):
        return list(self.docs)


def _match(doc, query):
    for k, cond in query.items():
        v = doc.get(k)
        if isinstance(cond, dict):
            if "$gte" in cond and not (v is not None and v >= cond["$gte"]):
                return False
            if "$nin" in cond and v in cond["$nin"]:
                return False
            if "$exists" in cond and (k in doc) != bool(cond["$exists"]):
                return False
        elif v != cond:
            return False
    return True


class Col:
    def __init__(self):
        self.docs = {}

    async def insert_one(self, d):
        self.docs[d["_id"]] = dict(d)

    async def find_one(self, q):
        return next((dict(d) for d in self.docs.values() if _match(d, q)), None)

    def find(self, q=None):
        return Cursor([dict(d) for d in self.docs.values() if _match(d, q or {})])

    async def update_one(self, q, u):
        d = self.docs[q["_id"]]
        d.update(u.get("$set", {}))


class DB:
    def __init__(self):
        self.c = {}

    def __getitem__(self, name):
        return self.c.setdefault(name, Col())


class FakeClient(ImpalaAirtimeClient):
    def __init__(self, balance=1000.0, fail_topup=None, fail_balance=False):
        super().__init__()
        self.balance = balance
        self.sent = []
        self.fail_topup = fail_topup
        self.fail_balance = fail_balance

    def get_payout_balance(self):
        if self.fail_balance:
            raise RuntimeError("provider down")
        return {"artm_balance": self.balance, "currency": "KES", "raw": {}}

    def topup_via_stk(self, amount_kes, paying_phone_number):
        if self.fail_topup:
            raise RuntimeError(self.fail_topup)
        self.sent.append((amount_kes, paying_phone_number))
        return {"message": "STK push sent", "apiKey": "must-not-be-stored", "requestId": "abc"}


def run(coro):
    return asyncio.run(coro)


def start(db, client, **kw):
    args = dict(amount_kes=100, paying_phone="0712345678", default_phone=None, commission=0.05, requested_by="u1", client=client)
    args.update(kw)
    return run(svc.start_topup(db, **args))


# ------------------------------------------------------------------ helpers
@pytest.mark.parametrize("raw,expected", [
    ("0712345678", "0712345678"), ("254712345678", "0712345678"), ("+254 712-345-678", "0712345678"),
    ("712345678", "0712345678"), ("0112345678", "0112345678"),
])
def test_normalize_phone(raw, expected):
    assert svc.normalize_kenyan_phone(raw) == expected


@pytest.mark.parametrize("bad", ["", "12345", "0812345678", "07123456789", "abc"])
def test_normalize_phone_rejects_garbage(bad):
    with pytest.raises(ValueError):
        svc.normalize_kenyan_phone(bad)


def test_airtel_hint_and_masking():
    assert svc.looks_like_airtel("0733253036") and svc.looks_like_airtel("0783253036")
    assert not svc.looks_like_airtel("0712345678")
    assert svc.mask_phone("0712345678") == "07******78"


def test_expected_float_is_the_linear_commission():
    assert svc.expected_float_kes(1000, 0.05) == 1050.0
    assert svc.expected_float_kes(500, 0.05) == 525.0


def test_provider_commissions_are_read_per_network_as_fractions():
    assert svc.provider_commissions({"percentageComission": {"safaricom": 0, "airtel": 5}}) == {"safaricom": 0.0, "airtel": 0.05}
    assert svc.provider_commissions({"percentageComission": "5"}) is None
    assert svc.provider_commissions({}) is None
    assert svc.provider_commissions(None) is None


def test_state_exposes_balance_and_provider_commissions_but_never_the_raw_account():
    class C(FakeClient):
        def get_payout_balance(self):
            return {"artm_balance": 16.0, "currency": "KES",
                    "raw": {"apiKey": "SECRET-KEY", "password": "HASH", "percentageComission": {"safaricom": 0, "airtel": 5}}}

    c = C()
    c.merchant_id, c.username, c.password = "2540013", "u", "p"
    state = run(svc.get_state(c, commission=0.05, default_phone="0712345678"))
    assert state["balanceKes"] == 16.0
    assert state["commissionByNetwork"] == {"safaricom": 0.0, "airtel": 0.05}
    assert state["accountNumber"] == "2540013"
    assert "SECRET-KEY" not in str(state) and "HASH" not in str(state)


# ------------------------------------------------------------------ start
def test_start_sends_the_push_and_records_a_pending_topup():
    db, c = DB(), FakeClient(balance=1000.0)
    doc = start(db, c, amount_kes=500)
    assert c.sent == [(500, "0712345678")]
    assert doc["status"] == "PENDING"
    assert doc["balanceBefore"] == 1000.0 and doc["expectedFloatKes"] == 525.0
    assert doc["payingPhoneMasked"] == "07******78"
    assert "0712345678" not in str(doc)            # the full number is never stored
    assert "apiKey" not in (doc["provider"] or {})  # credential-looking fields are dropped
    assert db[svc.COLLECTION].docs[doc["_id"]]["status"] == "PENDING"


def test_start_uses_the_default_phone_when_none_is_given():
    db, c = DB(), FakeClient()
    start(db, c, paying_phone=None, default_phone="0798765432")
    assert c.sent[0][1] == "0798765432"


@pytest.mark.parametrize("amount", [0, -5, svc.MAX_TOPUP_KES + 1, True, 10.5])
def test_start_rejects_bad_amounts_before_calling_the_provider(amount):
    c = FakeClient()
    with pytest.raises(ValueError):
        start(DB(), c, amount_kes=amount)
    assert c.sent == []


def test_start_requires_a_phone():
    with pytest.raises(ValueError):
        start(DB(), FakeClient(), paying_phone=None, default_phone=None)


def test_start_refuses_while_a_corridor_run_is_in_flight():
    db, c = DB(), FakeClient()
    run(db["corridor_runs"].insert_one({"_id": "RUN-1", "status": "AWAITING_OPPORTUNITY"}))
    with pytest.raises(svc.TopupConflict, match="RUN-1"):
        start(db, c)
    assert c.sent == []


def test_a_cancelled_run_does_not_block_a_topup_even_if_its_status_was_rewritten():
    """2026-10-07: a stray worker on another server kept writing AWAITING_MANUAL_MINT over a cancelled run."""
    db, c = DB(), FakeClient()
    run(db["corridor_runs"].insert_one({"_id": "RUN-9", "status": "AWAITING_MANUAL_MINT", "cancelledAt": "2026-10-07T11:09:13"}))
    assert start(db, c)["status"] == "PENDING" and len(c.sent) == 1


def test_a_finished_run_does_not_block_a_topup():
    db, c = DB(), FakeClient()
    run(db["corridor_runs"].insert_one({"_id": "RUN-0", "status": "COMPLETED"}))
    assert start(db, c)["status"] == "PENDING"


def test_a_new_push_retires_an_older_pending_one_so_one_credit_settles_only_one_record():
    db, c = DB(), FakeClient(balance=16.0)
    t0 = datetime(2026, 10, 7, 9, 0, 0)
    a = start(db, c, now=t0, amount_kes=100, paying_phone="0714073826")
    b = start(db, c, now=t0 + svc.DUPLICATE_WINDOW + timedelta(seconds=5), amount_kes=100, paying_phone="0725603575")

    old = db[svc.COLLECTION].docs[a["_id"]]
    assert old["status"] == "EXPIRED" and b["_id"] in old["note"]
    assert db[svc.COLLECTION].docs[b["_id"]]["status"] == "PENDING"

    c.balance = 121.0  # the second push is approved
    settled_b = run(svc.reconcile_topup(db, db[svc.COLLECTION].docs[b["_id"]], client=c))
    settled_a = run(svc.reconcile_topup(db, old, client=c))
    assert settled_b["status"] == "CONFIRMED" and settled_b["creditedFloatKes"] == 105.0
    assert settled_a["status"] == "EXPIRED"  # the retired one is not settled by b's credit


def test_start_refuses_a_second_push_while_one_is_waiting():
    db, c = DB(), FakeClient()
    start(db, c)
    with pytest.raises(svc.TopupConflict):
        start(db, c)
    assert len(c.sent) == 1


def test_start_does_nothing_when_the_balance_cannot_be_read():
    db, c = DB(), FakeClient(fail_balance=True)
    with pytest.raises(svc.ProviderError, match="nothing was sent"):
        start(db, c)
    assert c.sent == [] and db[svc.COLLECTION].docs == {}


def test_a_provider_rejection_is_recorded_as_failed_and_raised():
    db, c = DB(), FakeClient(fail_topup="400 Bad Request")
    with pytest.raises(svc.ProviderError, match="400"):
        start(db, c)
    (only,) = db[svc.COLLECTION].docs.values()
    assert only["status"] == "FAILED" and "400" in only["error"]


# ------------------------------------------------------------------ reconcile
def test_balance_rise_confirms_the_topup_and_reports_the_real_commission():
    db, c = DB(), FakeClient(balance=1000.0)
    doc = start(db, c, amount_kes=500)
    c.balance = 1525.0
    done = run(svc.reconcile_topup(db, doc, client=c))
    assert done["status"] == "CONFIRMED"
    assert done["creditedFloatKes"] == 525.0 and done["commissionKes"] == 25.0
    assert db[svc.COLLECTION].docs[doc["_id"]]["status"] == "CONFIRMED"


def test_no_balance_change_stays_pending_then_expires():
    db, c = DB(), FakeClient(balance=1000.0)
    t0 = datetime(2026, 10, 6, 12, 0, 0)
    doc = start(db, c, now=t0)
    assert run(svc.reconcile_topup(db, doc, client=c, now=t0 + timedelta(minutes=2)))["status"] == "PENDING"
    assert run(svc.reconcile_topup(db, doc, client=c, now=t0 + svc.PENDING_WINDOW + timedelta(seconds=1)))["status"] == "EXPIRED"


def test_a_failed_balance_read_never_flips_the_status():
    db, c = DB(), FakeClient(balance=1000.0)
    doc = start(db, c)
    c.fail_balance = True
    assert run(svc.reconcile_topup(db, doc, client=c))["status"] == "PENDING"


def test_a_settled_topup_is_not_reconciled_again():
    db, c = DB(), FakeClient(balance=1000.0)
    doc = start(db, c)
    c.balance = 1105.0
    first = run(svc.reconcile_topup(db, doc, client=c))
    c.balance = 9999.0
    assert run(svc.reconcile_topup(db, first, client=c))["creditedFloatKes"] == 105.0


def test_list_returns_newest_first_and_settles_pending_rows():
    db, c = DB(), FakeClient(balance=1000.0)
    t0 = datetime(2026, 10, 6, 12, 0, 0)
    a = start(db, c, now=t0, amount_kes=100)
    c.balance = 1105.0
    run(svc.reconcile_topup(db, a, client=c, now=t0 + timedelta(minutes=1)))
    b = start(db, c, now=t0 + timedelta(minutes=10), amount_kes=200)
    c.balance = 1315.0
    rows = run(svc.list_topups(db, 10, client=c))
    assert [r["_id"] for r in rows] == [b["_id"], a["_id"]]
    assert rows[0]["status"] == "CONFIRMED"


# ------------------------------------------------------------------ cancel
def test_cancel_marks_a_pending_topup_cancelled_and_frees_the_duplicate_guard():
    db, c = DB(), FakeClient(balance=16.0)
    t0 = datetime(2026, 10, 7, 9, 0, 0)
    a = start(db, c, now=t0)
    with pytest.raises(svc.TopupConflict):                      # still waiting -> a second push is refused
        start(db, c, now=t0 + timedelta(seconds=30))

    done = run(svc.cancel_topup(db, a["_id"], cancelled_by="admin-1", client=c, now=t0 + timedelta(seconds=40)))
    assert done["status"] == "CANCELLED" and done["cancelledBy"] == "admin-1"
    assert db[svc.COLLECTION].docs[a["_id"]]["status"] == "CANCELLED"

    again = start(db, c, now=t0 + timedelta(seconds=50))        # no longer blocked
    assert again["status"] == "PENDING"


def test_cancel_never_hides_a_credit_that_already_landed():
    db, c = DB(), FakeClient(balance=16.0)
    a = start(db, c, amount_kes=100)
    c.balance = 121.0                                           # approved just before the click
    with pytest.raises(ValueError, match="already confirmed"):
        run(svc.cancel_topup(db, a["_id"], client=c))
    assert db[svc.COLLECTION].docs[a["_id"]]["status"] == "CONFIRMED"


def test_cancel_rejects_unknown_and_settled_topups():
    db, c = DB(), FakeClient()
    with pytest.raises(LookupError):
        run(svc.cancel_topup(db, "TOPUP-NOPE", client=c))
    a = start(db, c)
    run(svc.cancel_topup(db, a["_id"], client=c))
    with pytest.raises(ValueError, match="already cancelled"):
        run(svc.cancel_topup(db, a["_id"], client=c))


def test_a_cancelled_topup_is_not_settled_by_a_later_credit():
    db, c = DB(), FakeClient(balance=16.0)
    a = start(db, c)
    run(svc.cancel_topup(db, a["_id"], client=c))
    c.balance = 121.0
    assert run(svc.reconcile_topup(db, db[svc.COLLECTION].docs[a["_id"]], client=c))["status"] == "CANCELLED"


# ------------------------------------------------------------------ account rotation
def test_topup_records_the_reseller_account_it_was_made_under():
    db, c = DB(), FakeClient()
    c.merchant_id = "001"
    assert start(db, c)["merchantId"] == "001"


def test_a_pending_topup_from_the_old_account_is_never_settled_against_the_new_accounts_balance():
    """The credentials were rotated: the old account's float was 16, the new account's is 1,680.
    Comparing them would report a made-up +1,664 KES credit."""
    db, old, new = DB(), FakeClient(balance=16.0), FakeClient(balance=1680.0)
    old.merchant_id, new.merchant_id = "2540013", "001"
    doc = start(db, old, amount_kes=10)

    settled = run(svc.reconcile_topup(db, doc, client=new))
    assert settled["status"] == "EXPIRED"
    assert settled["creditedFloatKes"] is None
    assert "different reseller account" in settled["note"]
    assert db[svc.COLLECTION].docs[doc["_id"]]["status"] == "EXPIRED"


def test_a_legacy_record_without_an_account_is_retired_not_settled():
    db, c = DB(), FakeClient(balance=1680.0)
    c.merchant_id = "001"
    legacy = {"_id": "TOPUP-OLD", "amountKes": 10, "balanceBefore": 16.0, "status": "PENDING", "createdAt": datetime.utcnow()}
    run(db[svc.COLLECTION].insert_one(legacy))
    assert run(svc.reconcile_topup(db, legacy, client=c))["status"] == "EXPIRED"


def test_same_account_still_settles_normally():
    db, c = DB(), FakeClient(balance=1000.0)
    c.merchant_id = "001"
    doc = start(db, c, amount_kes=500)
    c.balance = 1525.0
    assert run(svc.reconcile_topup(db, doc, client=c))["status"] == "CONFIRMED"


# ------------------------------------------------------------------ anomaly guard
def test_a_clean_credit_carries_no_anomaly_flag():
    db, c = DB(), FakeClient(balance=1000.0)
    doc = start(db, c, amount_kes=500)
    c.balance = 1525.0
    assert run(svc.reconcile_topup(db, doc, client=c)).get("anomaly") is None


def test_a_credit_far_from_the_expected_amount_is_flagged_not_presented_as_clean():
    db, c = DB(), FakeClient(balance=16.0)
    doc = start(db, c, amount_kes=10)
    c.balance = 1680.0                      # something else changed the balance
    done = run(svc.reconcile_topup(db, doc, client=c))
    assert done["status"] == "CONFIRMED" and done["anomaly"] and "expected" in done["anomaly"]


def test_a_missing_commission_is_flagged():
    """Paid 1,000 expecting 1,050 but only 1,000 arrived: the 5% commission did not apply."""
    db, c = DB(), FakeClient(balance=1000.0)
    doc = start(db, c, amount_kes=1000)
    c.balance = 2000.0
    done = run(svc.reconcile_topup(db, doc, client=c))
    assert done["status"] == "CONFIRMED" and done["creditedFloatKes"] == 1000.0
    assert done["anomaly"] and "1,050.00" in done["anomaly"]


@pytest.mark.parametrize("credited", [10.0, 10.5, 11.0])
def test_whole_shilling_rounding_on_a_small_topup_is_not_flagged(credited):
    db, c = DB(), FakeClient(balance=16.0)
    doc = start(db, c, amount_kes=10)       # expects 10.50
    c.balance = 16.0 + credited
    assert run(svc.reconcile_topup(db, doc, client=c)).get("anomaly") is None
