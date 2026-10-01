"""Provider adapter for ImpalaPay's real Reseller API
(airtime-api.impalapay.com) — replaces the earlier adapter, which pointed
at a different host/path shape (airtime.impalapay.com, /api/airtel/send)
that never matched this provider's real, documented contract (confirmed
the hard way: real PROCURE calls against the old paths returned a 402
that had nothing to do with float size, they were hitting the wrong API
entirely).

Real flow, per the reseller's own documentation:
    1. Float is topped up via M-Pesa Paybill 5600000 / account = merchant
       ID (or the STK-push shortcut, topup_via_stk below) — this is the
       ONLY place the reseller discount is captured: pay 1,000, receive
       1,050 of float. There is no per-sale discount.
    2. login() exchanges merchantId/username/password for a session token.
    3. generate_api_key() (session-token-authenticated) issues the API key
       that authenticates every send_airtime call. Generating a new key
       replaces the old one, so this is cached, not called every time.
    4. send_airtime() debits the float 1:1 — it does not create discount
       value itself, it just delivers already-discounted float as real
       airtime to a real phone number.

The provider credentials are read only from environment variables. This
adapter never logs credentials or full provider responses.
"""
from __future__ import annotations

import os
import threading
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()


class ImpalaAirtimeClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("IMPALA_RESELLER_BASE_URL", "https://airtime-api.impalapay.com").rstrip("/")
        self.merchant_id = os.getenv("IMPALA_RESELLER_MERCHANT_ID", "").strip()
        self.username = os.getenv("IMPALA_RESELLER_USERNAME", "").strip()
        self.password = os.getenv("IMPALA_RESELLER_PASSWORD", "")
        self.timeout = float(os.getenv("AIRTIME_PROVIDER_TIMEOUT_SECONDS", "20"))

        # Session token (login) and API key (generatetoken) are two
        # different credentials per the docs: the session token
        # authenticates login/details/transactions/topup; the API key
        # authenticates send_airtime only. Both cached in memory, not
        # re-fetched on every call.
        self._session_token: str | None = None
        # An operator can pre-provision a key via IMPALA_RESELLER_API_KEY
        # to avoid rotating it on every process restart (generating a new
        # key invalidates the old one — see generate_api_key's docstring).
        self._api_key: str | None = os.getenv("IMPALA_RESELLER_API_KEY", "").strip() or None
        self._lock = threading.Lock()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _require_credentials(self) -> None:
        if not (self.merchant_id and self.username and self.password):
            raise RuntimeError("IMPALA_RESELLER_MERCHANT_ID, IMPALA_RESELLER_USERNAME and IMPALA_RESELLER_PASSWORD are required")

    def login(self) -> str:
        """POST /api/app/reseller/login. Returns and caches the session
        token used by generate_api_key/get_payout_balance/get_transactions/
        topup_via_stk. Raises on a 400 (wrong merchantId/username/password)."""
        self._require_credentials()
        response = requests.post(
            self._url("/api/app/reseller/login"),
            json={"merchantId": self.merchant_id, "username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("token")
        if not token:
            raise RuntimeError(f"Reseller login did not return a token (got keys: {list(payload.keys())})")
        self._session_token = token
        return token

    def get_session_token(self) -> str:
        with self._lock:
            if self._session_token:
                return self._session_token
        return self.login()

    def generate_api_key(self) -> str:
        """GET /api/app/generatetoken, authenticated with the session
        token. Generating a new key replaces the old one on the provider's
        side — call this only when you actually need a fresh key, not on
        every request; get_api_key() below caches the result."""
        token = self.get_session_token()
        response = requests.get(
            self._url("/api/app/generatetoken"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        api_key = payload.get("apiKey") or payload.get("accessToken") or payload.get("key") or payload.get("token")
        if not api_key:
            raise RuntimeError(f"generatetoken did not return an apiKey (got keys: {list(payload.keys())})")
        with self._lock:
            self._api_key = api_key
        return api_key

    def get_api_key(self) -> str:
        """Prefers the existing key already visible on the account (GET
        /api/app/details returns it directly when apiEnabled is true)
        over calling generate_api_key() — that endpoint REPLACES the
        current key, which is unnecessary and disruptive if a working one
        already exists. Only generates a fresh one if the account truly
        has none yet."""
        with self._lock:
            if self._api_key:
                return self._api_key
        details = self.get_payout_balance()
        existing_key = details.get("raw", {}).get("apiKey")
        if existing_key:
            with self._lock:
                self._api_key = existing_key
            return existing_key
        return self.generate_api_key()

    def send_airtime(self, phone: str, amount_kes: int, reference: str | None = None) -> dict[str, Any]:
        """POST /api/app/airtime, authenticated with the API key (not the
        session token). Debits the float by exactly amount_kes — this call
        does NOT itself capture any discount; the float it draws from was
        already discounted at top-up time (see module docstring).

        Real documented error cases:
          400 "Insufficient balance" — top up first; response includes the
              current balance.
          400 — recipient's network not supported.
          401 — missing or malformed API key.
        `reference` is accepted for interface compatibility with the rest
        of this codebase (idempotency keys elsewhere) but this endpoint's
        real request body has no field for it — not sent."""
        if amount_kes <= 0:
            raise ValueError("Airtime amount must be greater than zero")
        api_key = self.get_api_key()
        try:
            response = requests.post(
                self._url("/api/app/airtime"),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"amount": int(amount_kes), "phoneNumber": phone},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            # Real response shape: {"error": bool, "message": str, "balance": number, "transactionId": str}
            # — success is error == False, NOT a "success" field.
            if payload.get("error"):
                raise RuntimeError(f"Provider rejected the airtime request: {payload.get('message')}")
            receipt_id = payload.get("transactionId") or reference
            return {
                "status": "success",
                "receipt_id": str(receipt_id) if receipt_id else None,
                "wallet_balance": payload.get("balance"),
                "message": payload.get("message"),
            }
        except requests.HTTPError as exc:
            # 400 "Insufficient balance" and network-not-supported both land
            # here with a real response body worth surfacing, not just the
            # bare HTTP status.
            detail = exc.response.text if exc.response is not None else str(exc)
            raise RuntimeError(f"ImpalaPay airtime send failed: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"ImpalaPay airtime send failed: {exc}") from exc

    def get_payout_balance(self) -> dict[str, Any]:
        """GET /api/app/details, authenticated with the session token.
        Returns the real airtime float (`accountBalance`) — the balance
        Brain_Engine.risk_engine.check_airtime_backing gates real IMC
        minting against."""
        token = self.get_session_token()
        response = requests.get(
            self._url("/api/app/details"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        balance = payload.get("accountBalance")
        if balance is None:
            raise RuntimeError(f"details response did not contain accountBalance (got keys: {list(payload.keys())})")
        return {"artm_balance": float(balance), "currency": "KES", "raw": payload}

    def get_transactions(self) -> dict[str, Any]:
        """GET /api/app/transactions, authenticated with the session
        token. Most recent airtime sales first — not called by the FSM
        today, exposed for reconciliation/audit tooling."""
        token = self.get_session_token()
        response = requests.get(
            self._url("/api/app/transactions"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def topup_via_stk(self, amount_kes: int, paying_phone_number: str) -> dict[str, Any]:
        """POST /api/app/topup, authenticated with the session token. Sends
        a real M-Pesa STK prompt to `paying_phone_number` (must be
        Safaricom) — approving it is what actually captures the reseller
        discount (pay amount_kes, receive amount_kes * (1 + discount) of
        float). This is the real, automatable discount-capturing action —
        NOT send_airtime, which only ever debits an already-funded float.
        Equivalent manual alternative: pay Paybill 5600000, account =
        merchant ID, directly."""
        if amount_kes <= 0:
            raise ValueError("Top-up amount must be greater than zero")
        token = self.get_session_token()
        response = requests.post(
            self._url("/api/app/topup"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"amount": int(amount_kes), "payingPhoneNumber": paying_phone_number},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def get_wholesale_discount_rate(self, network: str = "airtel") -> float:
        """NOT APPLICABLE to this real provider: the reseller discount is a
        single fixed rate applied only at top-up time (see module
        docstring), not a live, per-transaction rate this API exposes
        anywhere. Kept only so Brain_Engine.state_engine._execute_procure's
        USE_LIVE_DISCOUNT branch has something to call without crashing on
        a missing method — it should stay off (use_live_discount=False,
        the default) for this provider. Raises rather than fabricating a
        number if anyone does enable it against this client."""
        raise NotImplementedError(
            "ImpalaPay's real reseller API has no live per-transaction discount-rate endpoint — "
            "the discount is fixed and applied only at top-up. Do not enable use_live_discount "
            "for this provider; use the corridor's configured static discount instead."
        )


impala_airtime = ImpalaAirtimeClient()
