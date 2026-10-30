"""
Client for the Mamlaka Partner API (virtual cards).

Server-to-server only: the API key never reaches a browser. Async httpx, following services/airtel_client.py.

Error model (callers must treat these differently):
  MamlakaCardsError        - Mamlaka answered and refused (4xx). Definitive: the operation did NOT happen.
  MamlakaCardsUnavailable  - timeout / network / 5xx. The operation MAY have happened: never refund on this alone;
                             settle by replaying the same Idempotency-Key or reading /operations/{reference}.
"""

import os
from typing import Any, Optional

import httpx


class MamlakaCardsError(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"Mamlaka cards {status}: {detail}")
        self.status = status
        self.detail = detail

    @property
    def message(self) -> str:
        d = self.detail
        if isinstance(d, str):
            return d
        if isinstance(d, list):  # FastAPI validation errors
            return "; ".join(str(i.get("msg", i)) if isinstance(i, dict) else str(i) for i in d)
        if isinstance(d, dict):
            return str(d.get("message") or d)
        return str(d)


class MamlakaCardsUnavailable(Exception):
    pass


def _base_url() -> str:
    return os.environ.get("MAMLAKA_CARDS_BASE_URL", "").strip().rstrip("/")


def _api_key() -> str:
    return os.environ.get("MAMLAKA_CARDS_API_KEY", "").strip()


def configured() -> bool:
    return bool(_base_url() and _api_key())


async def _request(method: str, path: str, *, json: Optional[dict] = None, params: Optional[dict] = None,
                   idempotency_key: Optional[str] = None, files: Optional[dict] = None,
                   data: Optional[dict] = None, timeout: float = 30.0) -> Any:
    if not configured():
        raise MamlakaCardsUnavailable("Mamlaka cards is not configured")
    headers = {"X-API-Key": _api_key()}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, f"{_base_url()}{path}", json=json, params=params,
                                            headers=headers, files=files, data=data)
    except httpx.HTTPError as exc:
        raise MamlakaCardsUnavailable(f"{path}: {type(exc).__name__}") from exc
    if response.status_code >= 500:
        raise MamlakaCardsUnavailable(f"{path}: HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        raise MamlakaCardsUnavailable(f"{path}: non-JSON response")
    if response.status_code >= 400:
        raise MamlakaCardsError(response.status_code, body.get("detail", body) if isinstance(body, dict) else body)
    return body


# ---------------------------------------------------------------- customers + KYC
async def ensure_customer(external_id: str, full_name: str, email: str, phone: str) -> dict:
    return await _request("POST", "/customers", json={
        "externalId": external_id, "fullName": full_name, "email": email, "phone": phone})


async def get_customer(external_id: str) -> dict:
    return await _request("GET", f"/customers/{external_id}")


async def upload_document(external_id: str, kind: str, filename: str, content: bytes, content_type: str) -> dict:
    return await _request("POST", f"/customers/{external_id}/kyc/documents", data={"kind": kind},
                          files={"file": (filename, content, content_type)}, timeout=60.0)


async def submit_kyc(external_id: str, idempotency_key: str, details: dict) -> dict:
    return await _request("POST", f"/customers/{external_id}/kyc", json=details, idempotency_key=idempotency_key)


async def resubmit_kyc(external_id: str, holder_id: int, idempotency_key: str, details: dict) -> dict:
    return await _request("PUT", f"/customers/{external_id}/kyc/{holder_id}", json=details,
                          idempotency_key=idempotency_key)


async def kyc_status(external_id: str, refresh: bool = False) -> dict:
    return await _request("GET", f"/customers/{external_id}/kyc", params={"refresh": str(refresh).lower()})


# ---------------------------------------------------------------- pricing + operations
async def quote(kind: str, usd: str, card_type_id: int) -> dict:
    return await _request("GET", "/quote", params={"kind": kind, "amount": usd, "cardTypeId": card_type_id})


async def issue_card(external_id: str, idempotency_key: str, card_type_id: int, holder_id: int, usd: str) -> dict:
    return await _request("POST", f"/customers/{external_id}/cards", idempotency_key=idempotency_key,
                          json={"cardTypeId": card_type_id, "holderId": holder_id, "amount": usd})


async def deposit(external_id: str, idempotency_key: str, card_no: str, usd: str) -> dict:
    return await _request("POST", f"/customers/{external_id}/cards/deposit", idempotency_key=idempotency_key,
                          json={"cardNo": card_no, "amount": usd})


async def freeze(external_id: str, idempotency_key: str, card_no: str) -> dict:
    return await _request("POST", f"/customers/{external_id}/cards/freeze", idempotency_key=idempotency_key,
                          json={"cardNo": card_no})


async def unfreeze(external_id: str, idempotency_key: str, card_no: str) -> dict:
    return await _request("POST", f"/customers/{external_id}/cards/unfreeze", idempotency_key=idempotency_key,
                          json={"cardNo": card_no})


async def operation(reference: str, refresh: bool = False) -> dict:
    return await _request("GET", f"/operations/{reference}", params={"refresh": str(refresh).lower()})


# ---------------------------------------------------------------- cards
async def list_cards(external_id: str) -> dict:
    return await _request("GET", f"/customers/{external_id}/cards")


async def card_balance(external_id: str, card_no: str) -> dict:
    return await _request("GET", f"/customers/{external_id}/cards/{card_no}/balance")


async def card_details(external_id: str, card_no: str) -> dict:
    """SENSITIVE. Full number, expiry and CVV. Return it to the cardholder and drop it: never store or log it."""
    return await _request("POST", f"/customers/{external_id}/cards/details", json={"cardNo": card_no})
