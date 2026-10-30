"""
Callback authentication for the payment-gateway relays (/api/ramp/c2b/result, /b2c/result and
/api/v1/callbacks/collections|disbursements).

Run from backend/ with the project venv (no pytest or database needed):
    python -m unittest tests.test_callback_auth -v
"""

import hashlib
import hmac
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from routes import ramp

SECRET = "unit-test-hash-key"
BODY = '{"externalId":"TRADE_1","transactionStatus":"COMPLETE"}'
CLEAN = {k: "" for k in ("AIRTEL_CALLBACK_AUTH_ENABLED", "AIRTEL_CALLBACK_TOKEN", "AIRTEL_CALLBACK_HASH_KEY",
                         "ALLOW_UNAUTHENTICATED_CALLBACKS", "AIRTEL_CALLBACK_SIGNATURE_HEADER", "APP_ENV")}


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


def req(headers=None, query=None):
    return SimpleNamespace(headers=_Headers({k.lower(): v for k, v in (headers or {}).items()}),
                           query_params=query or {})


def sign(body=BODY, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def rejected(request, **env):
    with patch.dict(os.environ, {**CLEAN, **env}, clear=False):
        return ramp._callback_rejected(ramp._verify_airtel_callback_signature(request, BODY))


class CallbackAuthTests(unittest.TestCase):
    # --- the bug: an unconfigured deployment must not accept payment callbacks -------------------
    def test_unconfigured_callbacks_are_refused_even_without_app_env(self):
        self.assertTrue(rejected(req()))                                   # APP_ENV unset (the old hole)
        self.assertTrue(rejected(req(), APP_ENV="development"))
        self.assertTrue(rejected(req(), APP_ENV="production"))

    def test_explicit_dev_opt_out_works_only_outside_production(self):
        self.assertFalse(rejected(req(), ALLOW_UNAUTHENTICATED_CALLBACKS="true"))
        self.assertFalse(rejected(req(), ALLOW_UNAUTHENTICATED_CALLBACKS="true", APP_ENV="development"))
        self.assertTrue(rejected(req(), ALLOW_UNAUTHENTICATED_CALLBACKS="true", APP_ENV="production"))
        self.assertTrue(rejected(req(), ALLOW_UNAUTHENTICATED_CALLBACKS="true", APP_ENV="prod"))

    # --- HMAC (the gateway signs the raw body: X-Mamlaka-Signature: sha256=<hex>) ----------------
    def hmac_env(self, **extra):
        base = dict(AIRTEL_CALLBACK_AUTH_ENABLED="true", AIRTEL_CALLBACK_HASH_KEY=SECRET,
                    AIRTEL_CALLBACK_SIGNATURE_HEADER="X-Mamlaka-Signature")
        return {**base, **extra}

    def test_valid_signature_is_accepted(self):
        self.assertFalse(rejected(req({"X-Mamlaka-Signature": sign()}), **self.hmac_env()))

    def test_wrong_missing_or_tampered_signature_is_refused(self):
        self.assertTrue(rejected(req({"X-Mamlaka-Signature": sign(secret="other")}), **self.hmac_env()))
        self.assertTrue(rejected(req(), **self.hmac_env()))
        self.assertTrue(rejected(req({"X-Mamlaka-Signature": sign(body=BODY + " ")}), **self.hmac_env()))

    def test_signature_checking_with_no_shared_key_refuses_everything(self):
        env = self.hmac_env(AIRTEL_CALLBACK_HASH_KEY="")
        self.assertTrue(rejected(req({"X-Mamlaka-Signature": sign()}), **env))

    # --- shared token (gateways that cannot sign) -------------------------------------------------
    def test_shared_token_via_header_or_query(self):
        env = dict(AIRTEL_CALLBACK_TOKEN="tok-123")
        self.assertFalse(rejected(req({"X-Callback-Token": "tok-123"}), **env))
        self.assertFalse(rejected(req(query={"token": "tok-123"}), **env))
        self.assertTrue(rejected(req({"X-Callback-Token": "nope"}), **env))
        self.assertTrue(rejected(req(), **env))

    def test_when_both_are_configured_both_must_pass(self):
        env = self.hmac_env(AIRTEL_CALLBACK_TOKEN="tok-123")
        good = req({"X-Mamlaka-Signature": sign(), "X-Callback-Token": "tok-123"})
        bad_token = req({"X-Mamlaka-Signature": sign(), "X-Callback-Token": "x"})
        self.assertFalse(rejected(good, **env))
        self.assertTrue(rejected(bad_token, **env))


class RouteTableTests(unittest.TestCase):
    def test_each_callback_path_is_registered_once(self):
        seen = {}
        for r in list(ramp.router.routes) + list(ramp.callback_router.routes):
            for method in getattr(r, "methods", None) or ():
                if method == "POST":
                    seen.setdefault((ramp.router.prefix if r in ramp.router.routes else ramp.callback_router.prefix) + r.path, []).append(r.name)
        for path in ("/c2b/result", "/b2c/result", "/collections", "/disbursements"):
            matches = [names for key, names in seen.items() if key.endswith(path)]
            self.assertEqual(sum(len(n) for n in matches), 1, f"{path} registered {matches}")


if __name__ == "__main__":
    unittest.main()
