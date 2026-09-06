"""Provider adapter for Impala physical airtime delivery.

The provider credentials are read only from environment variables. This adapter
never logs credentials or full provider responses.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()


class ImpalaAirtimeClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("AIRTIME_PROVIDER_BASE_URL", "https://airtime.impalapay.com").rstrip("/")
        self.balance_url = os.getenv("AIRTIME_PROVIDER_BALANCE_URL", "").strip()
        self.balance_auth_base_url = os.getenv("AIRTIME_BALANCE_AUTH_BASE_URL", "https://payments.mam-laka.com").rstrip("/")
        self.balance_auth_username = os.getenv("AIRTIME_BALANCE_AUTH_USERNAME", "").strip()
        self.balance_auth_password = os.getenv("AIRTIME_BALANCE_AUTH_PASSWORD", "")
        self.token_path = os.getenv("AIRTIME_PROVIDER_TOKEN_PATH", "/api/auth/token")
        self.purchase_path = os.getenv("AIRTIME_PROVIDER_PURCHASE_PATH", "/deposit")
        self.balance_path = os.getenv("AIRTIME_PROVIDER_BALANCE_PATH", "/api/v1/read/payouts/balance")
        self.send_path = os.getenv("AIRTIME_PROVIDER_SEND_PATH", "/api/airtel/send")
        self.paybill = os.getenv("AIRTIME_PROVIDER_PAYBILL", "")
        self.account_number = os.getenv("AIRTIME_PROVIDER_ACCOUNT_NUMBER", "")
        self.api_key = os.getenv("AIRTIME_API_KEY", "").strip()
        self.api_secret = os.getenv("AIRTIME_API_SECRET", "").strip()
        self.customer_email = os.getenv("AIRTIME_PROVIDER_CUSTOMER_EMAIL", "")
        self.timeout = float(os.getenv("AIRTIME_PROVIDER_TIMEOUT_SECONDS", "20"))
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = threading.Lock()

    def _url(self, path: str) -> str:
        return path if path.startswith("http://") or path.startswith("https://") else f"{self.base_url}/{path.lstrip('/')}"

    def _require_credentials(self) -> None:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("AIRTIME_API_KEY and AIRTIME_API_SECRET are required")

    @staticmethod
    def _extract_token(payload: dict[str, Any]) -> tuple[str | None, int]:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        token = payload.get("access_token") or payload.get("token") or data.get("access_token") or data.get("token")
        expires_in = payload.get("expires_in") or payload.get("expiresIn") or 3600
        try:
            expires_in = int(expires_in)
        except (TypeError, ValueError):
            expires_in = 3600
        return str(token) if token else None, max(expires_in, 60)

    def get_access_token(self) -> str:
        self._require_credentials()
        with self._lock:
            if self._token and time.time() < self._token_expires_at - 30:
                return self._token
            response = requests.post(
                self._url(self.token_path),
                json={"apiKey": self.api_key, "apiSecret": self.api_secret},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("success") is False:
                raise RuntimeError("Provider rejected API credentials")
            token, expires_in = self._extract_token(payload)
            if not token:
                raise RuntimeError("Provider token response did not contain an access token")
            self._token = token
            self._token_expires_at = time.time() + expires_in
            return token


    def send_airtime(self, phone: str, amount_kes: int, reference: str) -> dict[str, Any]:
        """Disburse physical airtime and return a normalized provider receipt."""
        if amount_kes <= 0:
            raise ValueError("Airtime amount must be greater than zero")
        token = self.get_access_token()
        try:
            response = requests.post(
                self._url(self.send_path),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"number": phone, "amount": int(amount_kes), "email": self.customer_email},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("success") is False:
                raise RuntimeError("Provider rejected the airtime request")
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            if isinstance(data, dict):
                status = str(data.get("status") or payload.get("status") or "").lower()
                message = str(data.get("message") or payload.get("message") or "")
                if status in {"failed", "error", "rejected", "cancelled", "declined"} or "failed" in message.lower() or "error" in message.lower():
                    raise RuntimeError(f"Provider rejected the airtime request: {message or data}")
            receipt_id = data.get("requestRef") or data.get("airtelTransID") or data.get("transactionId") or data.get("receiptId") or reference
            return {
                "status": "success",
                "receipt_id": str(receipt_id),
                "request_ref": data.get("requestRef"),
                "provider_transaction_id": data.get("airtelTransID"),
                "wallet_balance": data.get("walletBalance"),
            }
        except Exception as exc:
            # ImpalaPay is the only airtime provider this platform actually
            # purchases airtime from — Mamlaka/Lipad is a separate M-Pesa
            # payment gateway (see services/safaricom_daraja.py), not an
            # airtime source, so it must never silently stand in here.
            raise RuntimeError(f"ImpalaPay airtime send failed: {exc}") from exc

    def get_payout_balance(self) -> dict[str, Any]:
        """Read the authenticated payout wallet's physical airtime balance."""
        if self.balance_auth_username and self.balance_auth_password:
            auth_response = requests.get(
                f"{self.balance_auth_base_url}/api/v1",
                auth=(self.balance_auth_username, self.balance_auth_password),
                timeout=self.timeout,
            )
            auth_response.raise_for_status()
            auth_payload = auth_response.json()
            token = auth_payload.get("token") or auth_payload.get("access_token")
            if not token:
                raise RuntimeError("Balance gateway token response did not contain a token")
        else:
            token = self.get_access_token()
        response = requests.get(
            self.balance_url or self._url(self.balance_path),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("success") is False:
            raise RuntimeError("Provider rejected the payout balance request")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if isinstance(data.get("Balances"), dict):
            data = data["Balances"]
        if data.get("artmBalance") is None:
            raise RuntimeError("Provider payout balance did not contain artmBalance")
        return {
            "artm_balance": float(data["artmBalance"]),
            "currency": "KES",
            "raw": {key: value for key, value in data.items() if key != "token"},
        }

    def purchase_reserve(self, amount_kes: int) -> dict[str, Any]:
        """Purchase airtime reserve for Jasiri and return the provider receipt."""
        if amount_kes <= 0:
            raise ValueError("Reserve purchase amount must be greater than zero")
        if not self.paybill or not self.account_number:
            raise RuntimeError("AIRTIME_PROVIDER_PAYBILL and AIRTIME_PROVIDER_ACCOUNT_NUMBER are required")
        token = self.get_access_token()
        response = requests.post(
            self._url(self.purchase_path),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"payBill": self.paybill, "accountNumber": self.account_number, "amount": str(int(amount_kes))},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("success") is False:
            raise RuntimeError("Provider rejected the reserve purchase")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        receipt_id = data.get("requestRef") or data.get("transactionId") or data.get("receiptId") or data.get("airtelTransID")
        if not receipt_id:
            raise RuntimeError("Provider reserve response did not contain a receipt reference")
        return {
            "status": "success",
            "receipt_id": str(receipt_id),
            "provider_transaction_id": data.get("airtelTransID"),
            "wallet_balance": data.get("walletBalance"),
        }


impala_airtime = ImpalaAirtimeClient()
