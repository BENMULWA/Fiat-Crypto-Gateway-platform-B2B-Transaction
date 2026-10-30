"""Regression tests for the 2026-10-12 retail go-live fixes.

Run from backend/ with the project venv (no pytest or database needed):

    python -m unittest tests.test_security_fixes -v

Each test names the bug it guards. They exercise the real functions; the only
fake is a tiny in-memory collection for the lock test.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("RATE_LIMIT_DISABLED", "false")

from fastapi import HTTPException  # noqa: E402
from pymongo.errors import DuplicateKeyError  # noqa: E402


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class RampValidation(unittest.TestCase):
    """Negative / NaN / unknown-asset amounts used to reach balance arithmetic."""

    def setUp(self):
        from routes.ramp import RampExecute, _validate_ramp_request
        self.Body, self.validate = RampExecute, _validate_ramp_request

    def body(self, **kw):
        d = dict(direction="swap", channel="Internal Ledger", from_asset="KES", to_asset="USDT",
                 amount=100.0, rate=1.0, fee=0.0, counterparty="")
        d.update(kw)
        return self.Body(**d)

    def test_rejects_bad_requests(self):
        for label, kw in {
            "negative": dict(amount=-5), "zero": dict(amount=0), "nan": dict(amount=float("nan")),
            "inf": dict(amount=float("inf")), "huge": dict(amount=1e12), "unknown asset": dict(to_asset="$where"),
            "self swap": dict(to_asset="KES"), "bad direction": dict(direction="sideways"),
            "bad min_receive": dict(min_receive=float("nan")),
        }.items():
            with self.subTest(label), self.assertRaises(HTTPException):
                self.validate(self.body(**kw))

    def test_accepts_normal_requests(self):
        self.validate(self.body())
        self.validate(self.body(min_receive=1.0))


class WalletGuards(unittest.TestCase):
    def test_debit_and_credit_reject_bad_amounts(self):
        import wallet_utils
        for bad in (-1, 0, float("nan"), float("inf")):
            with self.subTest(bad), self.assertRaises(HTTPException):
                run(wallet_utils.debit_wallet(None, "u", "KES", bad))
        for bad in (-1, float("nan")):
            with self.subTest(bad), self.assertRaises(HTTPException):
                run(wallet_utils.credit_wallet(None, "u", "KES", bad))
        run(wallet_utils.credit_wallet(None, "u", "KES", 0))  # zero is a no-op, must not touch db


class CallbackAuth(unittest.TestCase):
    """A forged provider callback could credit a deposit that was never paid."""

    class Req:
        def __init__(self, h=None, q=None):
            self.headers, self.query_params = h or {}, q or {}

    def setUp(self):
        self._env = {k: os.environ.pop(k, None) for k in ("AIRTEL_CALLBACK_AUTH_ENABLED", "AIRTEL_CALLBACK_TOKEN", "APP_ENV", "ALLOW_UNAUTHENTICATED_CALLBACKS")}
        from routes import ramp
        self.ramp = ramp

    def tearDown(self):
        for k, v in self._env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_production_without_any_auth_fails_closed(self):
        os.environ["APP_ENV"] = "production"
        self.assertTrue(self.ramp._verify_airtel_callback_signature(self.Req(), "")["failClosed"])

    def test_unconfigured_callbacks_fail_closed_in_every_environment(self):
        # Used to be permissive outside production, so a deployment that forgot APP_ENV accepted forged callbacks.
        v = self.ramp._verify_airtel_callback_signature
        self.assertTrue(v(self.Req(), "")["failClosed"])                  # APP_ENV unset
        os.environ["APP_ENV"] = "development"
        self.assertTrue(v(self.Req(), "")["failClosed"])

    def test_development_opt_out_is_explicit_and_ignored_in_production(self):
        v = self.ramp._verify_airtel_callback_signature
        os.environ["ALLOW_UNAUTHENTICATED_CALLBACKS"] = "true"
        self.assertFalse(v(self.Req(), "")["failClosed"])
        os.environ["APP_ENV"] = "production"
        self.assertTrue(v(self.Req(), "")["failClosed"])

    def test_token_must_match(self):
        os.environ["AIRTEL_CALLBACK_TOKEN"] = "s3cret"
        v = self.ramp._verify_airtel_callback_signature
        self.assertFalse(v(self.Req(), "")["tokenValid"])
        self.assertFalse(v(self.Req(q={"token": "nope"}), "")["tokenValid"])
        self.assertTrue(v(self.Req(q={"token": "s3cret"}), "")["tokenValid"])
        self.assertTrue(v(self.Req(h={"X-Callback-Token": "s3cret"}), "")["tokenValid"])

    def test_token_is_only_added_to_the_gateway_copy_of_the_url(self):
        os.environ["AIRTEL_CALLBACK_TOKEN"] = "s3cret"
        self.assertEqual(self.ramp._with_callback_token("https://x/cb"), "https://x/cb?token=s3cret")
        os.environ.pop("AIRTEL_CALLBACK_TOKEN")
        self.assertEqual(self.ramp._with_callback_token("https://x/cb"), "https://x/cb")


class RateBookFreshness(unittest.TestCase):
    """The rate book is an admin-typed table; a stale one must not keep quoting."""

    def setUp(self):
        self._env = {k: os.environ.pop(k, None) for k in ("APP_ENV", "RATE_BOOK_ENFORCE_FRESHNESS", "RATE_BOOK_MAX_AGE_HOURS")}
        from routes.treasury import assert_rate_book_fresh
        self.check = assert_rate_book_fresh

    def tearDown(self):
        for k, v in self._env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def book(self, age_hours, by="admin1"):
        return {"updatedBy": by, "updatedAt": datetime.utcnow() - timedelta(hours=age_hours), "refresh_interval_hours": 3}

    def test_not_enforced_outside_production(self):
        self.check(self.book(999, by=None))

    def test_enforced_in_production(self):
        os.environ["APP_ENV"] = "production"
        self.check(self.book(1))                      # fresh: ok
        with self.assertRaises(HTTPException):
            self.check(self.book(7))                  # older than 2x refresh (6h)
        with self.assertRaises(HTTPException):
            self.check(self.book(0, by=None))         # never set by an admin: still the seeded defaults


class RateLimiter(unittest.TestCase):
    def test_blocks_after_limit_and_is_per_key(self):
        import rate_limit
        os.environ["RATE_LIMIT_DISABLED"] = "false"
        rate_limit._hits.clear()
        for _ in range(3):
            rate_limit._check("t", "1.2.3.4", 3, 60)
        with self.assertRaises(HTTPException) as ctx:
            rate_limit._check("t", "1.2.3.4", 3, 60)
        self.assertEqual(ctx.exception.status_code, 429)
        rate_limit._check("t", "5.6.7.8", 3, 60)      # a different client is unaffected

    def test_client_ip_uses_rightmost_forwarded_entry(self):
        import rate_limit

        class R:
            headers = {"x-forwarded-for": "6.6.6.6, 9.9.9.9"}
            client = None
        self.assertEqual(rate_limit.client_ip(R()), "9.9.9.9")   # left entries are client-supplied


class TreasuryNonce(unittest.TestCase):
    """Two signers on the shared treasury key were handed the same nonce."""

    def test_consecutive_reservations_are_distinct_even_if_node_lags(self):
        import celo_nonce
        celo_nonce._last.clear()

        class Eth:
            def get_transaction_count(self, addr, block):
                assert block == "pending"
                return 7                                  # node hasn't seen our broadcasts yet

        class W3:
            eth = Eth()

        got = [celo_nonce.reserve_treasury_nonce(W3(), "0xabc") for _ in range(3)]
        self.assertEqual(got, [7, 8, 9])


class _Col:
    """Just enough of a Motor collection for request_lock."""
    def __init__(self):
        self.docs = {}

    async def insert_one(self, d):
        if d["_id"] in self.docs:
            raise DuplicateKeyError("dup")
        self.docs[d["_id"]] = dict(d)

    async def find_one_and_update(self, filt, upd):
        d = self.docs.get(filt["_id"])
        if d and d["expiresAt"] < filt["expiresAt"]["$lt"]:
            d.update(upd["$set"])
            return dict(d)
        return None

    async def delete_one(self, filt):
        d = self.docs.get(filt["_id"])
        if d and d["token"] == filt["token"]:
            del self.docs[filt["_id"]]


class RequestLock(unittest.TestCase):
    def test_second_request_is_refused_until_release_or_expiry(self):
        from request_lock import acquire_user_lock, release_user_lock
        db = {"request_locks": _Col()}

        async def scenario():
            t1 = await acquire_user_lock(db, "u1", "money_out")
            with self.assertRaises(HTTPException) as ctx:
                await acquire_user_lock(db, "u1", "money_out")          # double-click
            self.assertEqual(ctx.exception.status_code, 429)
            await acquire_user_lock(db, "u2", "money_out")              # other users unaffected
            await release_user_lock(db, "u1", "money_out", "wrong-token")
            with self.assertRaises(HTTPException):                      # wrong token can't release
                await acquire_user_lock(db, "u1", "money_out")
            await release_user_lock(db, "u1", "money_out", t1)
            await acquire_user_lock(db, "u1", "money_out")              # free again
            # a crashed holder's lock expires and can be taken over
            db["request_locks"].docs["money_out:u3"] = {
                "_id": "money_out:u3", "token": "dead", "expiresAt": datetime.utcnow() - timedelta(seconds=1)}
            await acquire_user_lock(db, "u3", "money_out")

        run(scenario())


class WebsocketAuth(unittest.TestCase):
    def test_identity_comes_from_the_token(self):
        from routes.auth import create_access_token
        from routes.realtime import _authenticate_ws
        tok = create_access_token({"sub": "user123", "workspaceId": "w"})
        self.assertEqual(run(_authenticate_ws(tok, None)), "user123")
        self.assertEqual(run(_authenticate_ws(tok, "user123")), "user123")
        self.assertIsNone(run(_authenticate_ws(tok, "someone-else")))   # can't subscribe as another user
        self.assertIsNone(run(_authenticate_ws("garbage", None)))
        self.assertIsNone(run(_authenticate_ws(None, "user123")))


class AuditSerialisation(unittest.TestCase):
    def test_documents_are_json_safe_and_secrets_stripped(self):
        from bson import ObjectId
        from routes.admin_audit_view import _clean
        doc = {"_id": ObjectId(), "at": datetime(2026, 1, 2, 3, 4, 5), "rawBody": "secret", "nested": {"o": ObjectId()}}
        out = _clean(doc, {"rawBody"})
        self.assertNotIn("rawBody", out)
        self.assertIsInstance(out["_id"], str)
        self.assertEqual(out["at"], "2026-01-02T03:04:05Z")
        self.assertIsInstance(out["nested"]["o"], str)


if __name__ == "__main__":
    unittest.main()
