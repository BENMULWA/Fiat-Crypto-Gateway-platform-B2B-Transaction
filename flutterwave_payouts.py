"""Real bank/card payouts via Flutterwave's Transfers API — the fiat
counterpart to celo_wallet.py/stellar_child_wallet.py's crypto rails. Shared
by both the retail withdrawal endpoint (routes/valora.py) and the admin
company-revenue payout endpoint (routes/otc_admin.py), so there's one place
that knows how to talk to Flutterwave for a bank transfer.

Reuses the same FLUTTERWAVE_API_KEY already configured for mobile money
payouts (see routes/swap_engine.py's settle_mobilemoney_via_flutterwave) —
no new secret needed, just a different Transfers API payload shape.

Before this goes live for real USD payouts, confirm with Flutterwave that
your merchant account has international/USD payout enabled — by default many
Flutterwave accounts are provisioned for local-currency payouts only (KES,
NGN, etc.), and a USD transfer request will simply be rejected by their API
until that's turned on for you.
"""
from __future__ import annotations

import os
import uuid

import httpx

FLUTTERWAVE_API_KEY = os.getenv("FLUTTERWAVE_API_KEY", "")
FLUTTERWAVE_BASE_URL = "https://api.flutterwave.com/v3"


def _headers() -> dict:
    if not FLUTTERWAVE_API_KEY:
        raise ValueError("FLUTTERWAVE_API_KEY is not configured.")
    return {"Authorization": f"Bearer {FLUTTERWAVE_API_KEY}", "Content-Type": "application/json"}


async def list_banks(country: str) -> list[dict]:
    """Live bank list + codes for a given ISO country code (e.g. 'KE', 'NG')
    straight from Flutterwave — never hardcode a bank code, since the same
    bank (e.g. UBA) has a different code in every country it operates in."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"{FLUTTERWAVE_BASE_URL}/banks/{country.upper()}", headers=_headers())
    result = response.json()
    if response.status_code not in (200, 201) or result.get("status") != "success":
        raise Exception(f"Flutterwave bank list error: {result.get('message', 'Unknown error')}")
    return [{"code": b.get("code"), "name": b.get("name")} for b in result.get("data", [])]


async def settle_bank_transfer(
    account_bank: str,
    account_number: str,
    account_name: str,
    currency: str,
    amount: float,
    narration: str = "Jasiri Withdrawal",
    reference: str | None = None,
) -> dict:
    """Broadcasts a real bank transfer via Flutterwave. Raises on any failure
    — callers are responsible for refunding the debited ledger balance if
    this raises, same contract as send_stellar_withdrawal_sync /
    send_usda / settle_crypto_on_celo elsewhere in this codebase."""
    payload = {
        "account_bank": account_bank,
        "account_number": account_number,
        "beneficiary_name": account_name,
        "amount": amount,
        "currency": currency.upper(),
        "narration": narration,
        "reference": reference or f"BANK-{uuid.uuid4().hex[:10].upper()}",
        "callback_url": os.getenv("FLUTTERWAVE_CALLBACK_URL", ""),
        "meta": {"platform": "jasiri"},
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(f"{FLUTTERWAVE_BASE_URL}/transfers", json=payload, headers=_headers())
    result = response.json()

    if response.status_code not in (200, 201):
        raise Exception(f"Flutterwave error: {result.get('message', 'Unknown error')}")
    if result.get("status") != "success":
        raise Exception(f"Flutterwave transfer failed: {result.get('message')}")

    data = result.get("data", {})
    return {
        "success": True,
        "reference": data.get("reference", payload["reference"]),
        "transfer_id": data.get("id"),
        "status": data.get("status", "pending"),
        "currency": currency.upper(),
        "amount": amount,
    }
