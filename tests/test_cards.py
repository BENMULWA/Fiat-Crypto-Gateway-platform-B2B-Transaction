"""
Virtual cards (Deposits -> Virtual Card): money rules, idempotency, settlement and the signed webhook.

No network, no MongoDB: a small in-memory database stands in for Mongo and the Mamlaka client is stubbed.
Run from backend/ with the project venv:
    python -m unittest tests.test_cards -v
"""

import hashlib
import hmac
import json
import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from pymongo.errors import DuplicateKeyError

from routes import cards as card_routes
from services import card_ops as ops
from services import mamlaka_cards as partner

ENV = {"CARD_FEATURE_ENABLED": "true", "MAMLAKA_CARDS_BASE_URL": "http://partner.test/api/v1/partner",
       "MAMLAKA_CARDS_API_KEY": "mk_live_test", "MAMLAKA_CARDS_WEBHOOK_SECRET": "whsec_test",
       "CARD_MARGIN_PCT": "2.0", "CARD_MIN_USD": "10", "CARD_MAX_USD": "50", "CARD_TYPE_ID": "111078"}
USER = "user-1"


# ---------------------------------------------------------------- minimal in-memory Mongo
def _match(doc, flt):
    for key, cond in flt.items():
        value = doc.get(key)
        if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
            for op, arg in cond.items():
                if op == "$in" and value not in arg: return False
                if op == "$ne" and (value == arg or (isinstance(value, list) and arg in value)): return False
                if op == "$gte" and not (value is not None and value >= arg): return False
                if op == "$exists" and (key in doc) != arg: return False
        elif isinstance(value, list) and not isinstance(cond, list):
            if cond not in value: return False
        elif value != cond:
            return False
    return True


class Result:
    def __init__(self, matched=0, modified=0):
        self.matched_count, self.modified_count = matched, modified


class Cursor:
    def __init__(self, docs): self.docs = list(docs)
    def sort(self, key, direction=1): self.docs.sort(key=lambda d: d.get(key), reverse=direction < 0); return self
    def limit(self, n): self.docs = self.docs[:n]; return self
    async def to_list(self, length=None): return self.docs
    def __aiter__(self):
        async def gen():
            for d in self.docs: yield d
        return gen()


class Collection:
    def __init__(self): self.docs = []

    async def insert_one(self, doc):
        if any(d.get("_id") == doc.get("_id") for d in self.docs): raise DuplicateKeyError("dup")
        self.docs.append(dict(doc))

    async def find_one(self, flt=None, projection=None):
        return next((dict(d) for d in self.docs if _match(d, flt or {})), None)

    def find(self, flt=None, projection=None): return Cursor(dict(d) for d in self.docs if _match(d, flt or {}))

    async def update_one(self, flt, update, upsert=False):
        doc = next((d for d in self.docs if _match(d, flt)), None)
        if doc is None:
            if not upsert: return Result()
            doc = {k: v for k, v in flt.items() if not isinstance(v, dict)}
            doc.update(update.get("$setOnInsert", {}))
            self.docs.append(doc)
        before = json.dumps(doc, default=str, sort_keys=True)
        for k, v in update.get("$set", {}).items(): doc[k] = v
        for k, v in update.get("$inc", {}).items(): doc[k] = doc.get(k, 0) + v
        for k, v in update.get("$addToSet", {}).items():
            doc.setdefault(k, [])
            if v not in doc[k]: doc[k].append(v)
        return Result(1, int(before != json.dumps(doc, default=str, sort_keys=True)))


class FakeDb(dict):
    def __getitem__(self, name):
        if name not in self: self[name] = Collection()
        return dict.__getitem__(self, name)


def signed(body: dict, secret="whsec_test"):
    raw = json.dumps(body).encode()
    return raw, "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


class CardCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env = patch.dict(os.environ, ENV); env.start(); self.addCleanup(env.stop)
        self.db = FakeDb()
        await self.db["retail_wallets"].insert_one({"_id": "w1", "userId": USER, "KES": 3000.0})
        await self.db["card_profiles"].insert_one(
            {"_id": USER, "kycStatus": "pass_audit", "holderId": 900, "cardTypeId": 111078})
        self.pushes = []

        async def push(user_id, message): self.pushes.append((user_id, message))
        self.patch(ops.broadcast_manager, "send_user", push)
        self.calls = {"issue": [], "deposit": []}
        self.partner_state = {"issue": {"merchantOrderNo": "REF1", "status": "PENDING", "cardNo": "C1"},
                              "operation": {"status": "PENDING"}}

        async def quote(kind, usd, card_type): return {"kes": "1554.00", "cardFeeUsd": "2"}

        async def issue(external_id, key, card_type, holder, usd):
            self.calls["issue"].append(key)
            out = self.partner_state["issue"]
            if isinstance(out, Exception): raise out
            return out

        async def deposit(external_id, key, card_no, usd):
            self.calls["deposit"].append(key)
            out = self.partner_state.get("deposit", {"merchantOrderNo": "REF2", "status": "PENDING"})
            if isinstance(out, Exception): raise out
            return out

        async def operation(reference, refresh=False):
            out = self.partner_state["operation"]
            if isinstance(out, Exception): raise out
            return out

        self.patch(partner, "quote", quote); self.patch(partner, "issue_card", issue)
        self.patch(partner, "deposit", deposit); self.patch(partner, "operation", operation)

    def patch(self, obj, name, value):
        p = patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)

    def kes(self): return self.db["retail_wallets"].docs[0]["KES"]
    async def op(self, op_id): return await self.db[ops.OPS].find_one({"_id": op_id})

    async def issue_now(self, key="issue-key-0001", usd="10"):
        return await ops.start_operation(self.db, USER, ops.ISSUE, ops.parse_usd(usd), key)


class PricingAndLimits(CardCase):
    async def test_user_pays_provider_cost_plus_margin(self):
        pr = await ops.price(ops.ISSUE, Decimal("10"))
        self.assertEqual((pr["costKes"], pr["chargeKes"], pr["marginKes"]), ("1554.00", "1585.08", "31.08"))

    def test_amounts_are_decimal_checked_and_limited(self):
        self.assertEqual(ops.parse_usd("10"), Decimal("10.00"))
        from fastapi import HTTPException
        for bad in ("abc", "10.005", "9.99", "50.01", "0", "-5", "nan"):
            with self.assertRaises(HTTPException, msg=bad): ops.parse_usd(bad)


class Issuing(CardCase):
    async def test_issue_debits_first_then_success_keeps_the_money_spent(self):
        op = await self.issue_now()
        self.assertEqual(op["status"], ops.SUBMITTED)
        self.assertAlmostEqual(self.kes(), 3000 - 1585.08, places=2)
        self.assertEqual((op["partnerRef"], op["cardNo"]), ("REF1", "C1"))

        raw = {"id": "evt-1", "type": "operation.updated", "data": {"reference": "REF1", "status": "SUCCESS", "customerId": USER}}
        self.assertEqual(await ops.handle_event(self.db, raw), "processed")
        self.assertEqual((await self.op(op["_id"]))["status"], ops.SUCCESS)
        self.assertAlmostEqual(self.kes(), 3000 - 1585.08, places=2)               # spent, not refunded
        self.assertEqual(await ops.handle_event(self.db, raw), "duplicate")        # replayed event is a no-op
        self.assertIn("card_operation_succeeded", [m["reason"] for _, m in self.pushes])

    async def test_retried_request_charges_and_sends_once(self):
        first = await self.issue_now(key="same-key-1234")
        again = await self.issue_now(key="same-key-1234")
        self.assertEqual(first["_id"], again["_id"])
        self.assertEqual(len(self.calls["issue"]), 1)
        self.assertAlmostEqual(self.kes(), 3000 - 1585.08, places=2)

    async def test_insufficient_balance_stops_before_the_provider(self):
        await self.db["retail_wallets"].update_one({"_id": "w1"}, {"$set": {"KES": 100.0}})
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx: await self.issue_now()
        self.assertEqual(ctx.exception.status_code, 402)
        self.assertEqual(self.calls["issue"], [])
        self.assertEqual(self.kes(), 100.0)

    async def test_card_needs_an_approved_application(self):
        await self.db["card_profiles"].update_one({"_id": USER}, {"$set": {"kycStatus": "wait_audit"}})
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx: await self.issue_now()
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual((self.kes(), self.calls["issue"]), (3000.0, []))


class Failures(CardCase):
    async def test_definitive_refusal_refunds_and_hides_provider_internals(self):
        self.partner_state["issue"] = partner.MamlakaCardsError(402, "Insufficient float balance. Top up your Mamlaka float.")
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx: await self.issue_now()
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertNotIn("float", ctx.exception.detail.lower())
        self.assertEqual(self.kes(), 3000.0)
        op = (await self.db[ops.OPS].find_one({}))
        self.assertEqual(op["status"], ops.REFUNDED)

    async def test_unknown_outcome_never_refunds_and_settles_by_reading_back(self):
        self.partner_state["issue"] = partner.MamlakaCardsUnavailable("timeout")
        op = await self.issue_now()
        self.assertEqual(op["status"], ops.DEBITED)
        self.assertAlmostEqual(self.kes(), 3000 - 1585.08, places=2)               # held, not refunded

        self.partner_state["issue"] = {"merchantOrderNo": "REF1", "status": "SUCCESS", "cardNo": "C1"}
        settled = await ops.settle(self.db, op, force=True)                        # replays the SAME key
        self.assertEqual((settled["status"], settled["cardNo"]), (ops.SUCCESS, "C1"))
        self.assertEqual(self.calls["issue"][0], self.calls["issue"][1])
        self.assertAlmostEqual(self.kes(), 3000 - 1585.08, places=2)

    async def test_provider_failure_refunds_exactly_once_across_every_path(self):
        op = await self.issue_now()
        failed = {"id": "evt-f", "type": "operation.updated", "data": {"reference": "REF1", "status": "FAILED", "customerId": USER}}
        await ops.handle_event(self.db, failed)
        await ops.handle_event(self.db, {**failed, "id": "evt-f2"})                # a second, different event
        self.partner_state["operation"] = {"status": "FAILED"}
        await ops.settle(self.db, await self.op(op["_id"]), force=True)             # and a poll
        await ops.settle_open_operations(self.db)                                  # and the reconcile pass
        self.assertEqual(self.kes(), 3000.0)                                       # refunded once, not 2-4x
        self.assertEqual((await self.op(op["_id"]))["status"], ops.REFUNDED)

    async def test_refund_marker_is_idempotent_even_if_the_status_write_was_lost(self):
        op = await self.issue_now()
        await ops.refund(self.db, op, "first")
        await self.db[ops.OPS].update_one({"_id": op["_id"]}, {"$set": {"status": ops.SUBMITTED}})   # simulate the lost write
        await ops.refund(self.db, await self.op(op["_id"]), "again")
        self.assertEqual(self.kes(), 3000.0)

    async def test_pending_stays_pending_until_the_provider_decides(self):
        op = await self.issue_now()
        settled = await ops.settle(self.db, op, force=True)
        self.assertEqual(settled["status"], ops.SUBMITTED)
        self.assertAlmostEqual(self.kes(), 3000 - 1585.08, places=2)

    async def test_a_charge_whose_debit_is_unconfirmed_goes_to_review_not_to_a_guess(self):
        await self.db[ops.OPS].insert_one({"_id": "CARD_X", "userId": USER, "kind": ops.ISSUE, "usd": "10.00",
                                           "chargeKes": "1585.08", "status": ops.PENDING_DEBIT, "request": {},
                                           "createdAt": datetime.utcnow() - timedelta(minutes=10)})
        settled = await ops.settle(self.db, await self.op("CARD_X"), force=True)
        self.assertEqual(settled["status"], ops.REVIEW)
        self.assertEqual(self.kes(), 3000.0)                                       # no automatic money movement


class Funding(CardCase):
    async def test_loading_a_card_is_a_separate_charge_that_settles_the_same_way(self):
        op = await ops.start_operation(self.db, USER, ops.FUND, Decimal("20"), "fund-key-0001", card_no="C1")
        self.assertEqual((op["status"], op["cardNo"]), (ops.SUBMITTED, "C1"))
        await ops.handle_event(self.db, {"id": "evt-d", "type": "operation.updated",
                                         "data": {"reference": "REF2", "status": "FAILED", "customerId": USER}})
        self.assertEqual(self.kes(), 3000.0)


class KycFlow(CardCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.db["card_profiles"].docs.clear()
        self.sent = {}

        async def ensure(*a, **k): self.sent["customer"] = a; return {}
        async def upload(external_id, kind, name, content, ctype): return {"fileId": f"file-{kind}"}
        async def submit(external_id, key, details): self.sent["details"] = details; return {"data": {"holderId": 901, "status": "wait_audit"}}
        async def resubmit(external_id, holder_id, key, details): self.sent["resubmit"] = holder_id; return {"data": {"holderId": holder_id, "status": "wait_audit"}}
        for name, fn in (("ensure_customer", ensure), ("upload_document", upload), ("submit_kyc", submit), ("resubmit_kyc", resubmit)):
            self.patch(partner, name, fn)
        self.files = {"front": ("f.jpg", b"x", "image/jpeg"), "selfie": ("s.jpg", b"x", "image/jpeg")}
        self.user = {"_id": USER, "email": "a@b.co"}
        self.details = {"firstName": "Jane", "lastName": "Doe", "areaCode": "+254", "mobile": "712345678", "email": "j@d.co"}

    async def test_submit_creates_the_customer_uploads_and_records_only_status(self):
        out = await ops.submit_kyc(self.db, self.user, dict(self.details), self.files, "kyc-key-0001")
        self.assertEqual(out, {"holderId": 901, "status": "wait_audit"})
        self.assertEqual(self.sent["details"]["idFrontId"], "file-front")
        self.assertEqual(self.sent["details"]["idHoldId"], "file-selfie")
        profile = await ops.get_profile(self.db, USER)
        self.assertEqual((profile["holderId"], profile["kycStatus"]), (901, "wait_audit"))
        self.assertNotIn("firstName", json.dumps(profile, default=str))              # no personal data kept

    async def test_a_pending_or_approved_application_cannot_be_resubmitted(self):
        await ops.submit_kyc(self.db, self.user, dict(self.details), self.files, "kyc-key-0001")
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await ops.submit_kyc(self.db, self.user, dict(self.details), self.files, "kyc-key-0002")
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_a_rejected_application_is_corrected_on_the_same_cardholder(self):
        await ops.submit_kyc(self.db, self.user, dict(self.details), self.files, "kyc-key-0001")
        await self.db["card_profiles"].update_one({"_id": USER}, {"$set": {"kycStatus": "reject"}})
        await ops.submit_kyc(self.db, self.user, dict(self.details), self.files, "kyc-key-0003")
        self.assertEqual(self.sent["resubmit"], 901)

    async def test_the_decision_arrives_by_signed_event(self):
        await ops.submit_kyc(self.db, self.user, dict(self.details), self.files, "kyc-key-0001")
        await ops.handle_event(self.db, {"id": "evt-k", "type": "kyc.status_changed",
                                         "data": {"customerId": USER, "status": "pass_audit", "approved": True}})
        self.assertEqual((await ops.get_profile(self.db, USER))["kycStatus"], "pass_audit")


class Webhook(CardCase):
    def test_signature_check(self):
        raw, sig = signed({"id": "e"})
        self.assertTrue(card_routes.signature_valid(raw, sig, "whsec_test"))
        self.assertFalse(card_routes.signature_valid(raw, sig, "other"))
        self.assertFalse(card_routes.signature_valid(raw + b" ", sig, "whsec_test"))
        self.assertFalse(card_routes.signature_valid(raw, "", "whsec_test"))
        self.assertFalse(card_routes.signature_valid(raw, sig, ""))

    async def http(self):
        app = FastAPI()
        app.include_router(card_routes.webhook_router)
        app.include_router(card_routes.router)
        app.dependency_overrides[card_routes.get_db] = lambda: self.db
        app.dependency_overrides[card_routes.get_current_user] = lambda: {"_id": USER, "email": "a@b.co"}
        app.dependency_overrides[card_routes.get_verified_current_user] = lambda: {"_id": USER, "email": "a@b.co"}
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    async def test_only_correctly_signed_events_are_accepted(self):
        body = {"id": "evt-w", "type": "kyc.status_changed", "data": {"customerId": USER, "status": "reject", "reason": "x"}}
        raw, sig = signed(body)
        async with await self.http() as c:
            bad = await c.post("/api/v1/webhooks/mamlaka-cards", content=raw, headers={"X-Mamlaka-Signature": "sha256=00"})
            none = await c.post("/api/v1/webhooks/mamlaka-cards", content=raw)
            self.assertEqual((bad.status_code, none.status_code), (401, 401))
            self.assertIsNone(await self.db["card_events"].find_one({"_id": "evt-w"}))
            ok = await c.post("/api/v1/webhooks/mamlaka-cards", content=raw, headers={"X-Mamlaka-Signature": sig})
            self.assertEqual(ok.status_code, 200)
        self.assertEqual((await ops.get_profile(self.db, USER))["kycStatus"], "reject")

    async def test_an_unconfigured_secret_refuses_every_event(self):
        with patch.dict(os.environ, {"MAMLAKA_CARDS_WEBHOOK_SECRET": ""}):
            raw, sig = signed({"id": "e"})
            async with await self.http() as c:
                r = await c.post("/api/v1/webhooks/mamlaka-cards", content=raw, headers={"X-Mamlaka-Signature": sig})
            self.assertEqual(r.status_code, 503)

    async def test_issue_requires_an_idempotency_key_and_the_step_up(self):
        step_up = AsyncMock()
        self.patch(card_routes, "verify_withdrawal_2fa", step_up)
        async with await self.http() as c:
            no_key = await c.post("/api/cards/issue", json={"usd": "10"})
            self.assertEqual(no_key.status_code, 422)
            step_up.assert_not_awaited()
            r = await c.post("/api/cards/issue", json={"usd": "10", "otp_session_id": "s", "otp_code": "1", "totp_code": "2"},
                             headers={"Idempotency-Key": "route-key-0001"})
        self.assertEqual(r.status_code, 200, r.text)
        step_up.assert_awaited_once()
        self.assertEqual(r.json()["status"], ops.SUBMITTED)
        self.assertAlmostEqual(self.kes(), 3000 - 1585.08, places=2)

    async def test_a_failed_step_up_charges_nothing(self):
        from fastapi import HTTPException
        self.patch(card_routes, "verify_withdrawal_2fa", AsyncMock(side_effect=HTTPException(400, "bad code")))
        async with await self.http() as c:
            r = await c.post("/api/cards/issue", json={"usd": "10"}, headers={"Idempotency-Key": "route-key-0002"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual((self.kes(), self.calls["issue"]), (3000.0, []))

    async def test_card_details_need_the_step_up_and_are_never_cached(self):
        self.patch(card_routes, "verify_withdrawal_2fa", AsyncMock())
        self.patch(partner, "card_details", AsyncMock(return_value={"cardNumber": "4111111111111111", "cvv": "123"}))
        async with await self.http() as c:
            r = await c.post("/api/cards/C1/details", json={"otp_session_id": "s", "otp_code": "1", "totp_code": "2"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
