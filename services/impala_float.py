"""Standalone top-up of the ImpalaPay reseller float by M-Pesa STK push.

This is the same real action a corridor run's PROCURE step takes
(services.impala_airtime.topup_via_stk), but started by an operator on its own,
so the float can be topped up without starting a corridor. Per the reseller's
documentation, paying in X credits X * (1 + commission) of airtime float within
seconds (1,000 at 5% gives 1,050); that commission is the whole margin.

How a top-up is tracked: the STK response says only that a prompt was sent, not
that anyone approved it. So each top-up is stored as PENDING with the float
balance seen just before the push, and is confirmed only when the real payout
balance later rises above that figure (the same real-delta approach PROCURE uses).
Nothing here writes to the corridor ledger: a standalone top-up is not a corridor
leg, and booking it as one would corrupt the per-node accounting.

Safety rails:
- admin-only (enforced by the route),
- a hard per-top-up cap (IMPALA_TOPUP_MAX_KES, default 50,000),
- refuses while a corridor run is in flight, because that run measures the same
  balance to work out what its own top-up delivered,
- refuses a second push while one is still waiting for approval.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Optional

from services.impala_airtime import ImpalaAirtimeClient, impala_airtime

COLLECTION = "float_topups"
RUNS_COLLECTION = "corridor_runs"
TERMINAL_RUN_STATES = ["COMPLETED", "HALTED"]

PAYBILL = os.getenv("IMPALA_RESELLER_PAYBILL", "5600000").strip() or "5600000"
MAX_TOPUP_KES = int(os.getenv("IMPALA_TOPUP_MAX_KES", "50000"))
# An STK prompt stays live for a short while; past this with no balance rise, call it expired.
PENDING_WINDOW = timedelta(minutes=int(os.getenv("IMPALA_TOPUP_PENDING_MINUTES", "15")))
DUPLICATE_WINDOW = timedelta(seconds=180)
# How far the real credit may differ from amount * (1 + commission) before it is flagged: 1% or 1 KES,
# whichever is larger (the provider rounds to whole shillings). Tight on purpose: a 5% commission that did
# not apply must not pass as a clean success.
ANOMALY_TOLERANCE_PCT = 0.01
ANOMALY_TOLERANCE_KES = 1.0

# Number ranges that are normally Airtel. Number portability means a prefix is only a hint.
_AIRTEL_PREFIX = re.compile(r"^0(73|78|75[0-6])")


class TopupConflict(ValueError):
    """Another top-up or a corridor run is already using the float."""


class ProviderError(RuntimeError):
    """ImpalaPay refused or could not be reached."""


# ---------------------------------------------------------------- pure helpers
def normalize_kenyan_phone(raw: str) -> str:
    """Returns the national form 07XXXXXXXX / 01XXXXXXXX, or raises ValueError."""
    digits = re.sub(r"[^0-9]", "", raw or "")
    if digits.startswith("254"):
        digits = "0" + digits[3:]
    elif len(digits) == 9:
        digits = "0" + digits
    if not re.fullmatch(r"0[17]\d{8}", digits):
        raise ValueError("Enter a Kenyan mobile number such as 0712345678 or 254712345678.")
    return digits


def mask_phone(phone: str) -> str:
    return phone[:2] + "*" * (len(phone) - 4) + phone[-2:] if len(phone) >= 6 else "***"


def looks_like_airtel(phone: str) -> bool:
    return bool(_AIRTEL_PREFIX.match(phone))


def expected_float_kes(amount_kes: int, commission: float) -> float:
    return round(amount_kes * (1 + commission), 2)


def provider_commissions(raw: Any) -> Optional[dict[str, float]]:
    """The account's own commission by network, as fractions: {'airtel': 0.05, 'safaricom': 0.0}.
    ImpalaPay reports percentages in `percentageComission` on GET /api/app/details."""
    pc = raw.get("percentageComission") if isinstance(raw, dict) else None
    if not isinstance(pc, dict):
        return None
    out = {str(k).lower(): float(v) / 100 for k, v in pc.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return out or None


def _safe_provider_payload(payload: Any) -> Optional[dict]:
    """Keep the provider's reply for the record, minus anything that looks like a credential."""
    if not isinstance(payload, dict):
        return None
    banned = ("token", "key", "secret", "password", "authorization")
    out = {}
    for k, v in payload.items():
        if any(b in str(k).lower() for b in banned):
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[str(k)] = v
    return out or None


# ---------------------------------------------------------------- operations
async def get_state(client: ImpalaAirtimeClient, *, commission: float, default_phone: Optional[str]) -> dict[str, Any]:
    configured = bool(client.merchant_id and client.username and client.password)
    state: dict[str, Any] = {
        "configured": configured,
        "paybill": PAYBILL,
        "accountNumber": client.merchant_id or None,
        "commission": commission,
        "maxTopupKes": MAX_TOPUP_KES,
        "defaultPhoneMasked": mask_phone(normalize_kenyan_phone(default_phone)) if default_phone else None,
        "defaultPhoneLooksAirtel": looks_like_airtel(normalize_kenyan_phone(default_phone)) if default_phone else False,
        "balanceKes": None,
        "commissionByNetwork": None,
    }
    if configured:
        try:
            bal = await asyncio.to_thread(client.get_payout_balance)
            state["balanceKes"] = bal["artm_balance"]
            state["commissionByNetwork"] = provider_commissions(bal.get("raw"))
        except Exception as exc:  # surfaced to the operator, never swallowed
            raise ProviderError(f"Could not read the ImpalaPay balance: {exc}") from exc
    return state


async def start_topup(
    db,
    *,
    amount_kes: int,
    paying_phone: Optional[str],
    default_phone: Optional[str],
    commission: float,
    requested_by: Optional[str],
    client: ImpalaAirtimeClient = impala_airtime,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    now = now or datetime.utcnow()

    if isinstance(amount_kes, bool) or not isinstance(amount_kes, int) or amount_kes < 1:
        raise ValueError("Amount must be a whole number of shillings, at least 1.")
    if amount_kes > MAX_TOPUP_KES:
        raise ValueError(f"Amount is above the per-top-up limit of {MAX_TOPUP_KES:,} KES.")
    phone = normalize_kenyan_phone(paying_phone or default_phone or "")  # raises if none was given

    active_run = await db[RUNS_COLLECTION].find_one({"status": {"$nin": TERMINAL_RUN_STATES}})
    if active_run:
        raise TopupConflict(
            f"Corridor run {active_run.get('_id')} is in flight. It measures the same float balance, "
            "so a separate top-up would be miscounted. Wait for it to finish."
        )
    recent_pending = await db[COLLECTION].find_one({"status": "PENDING", "createdAt": {"$gte": now - DUPLICATE_WINDOW}})
    if recent_pending:
        raise TopupConflict(
            f"Top-up {recent_pending.get('_id')} is still waiting for approval on the phone. "
            "Approve it, or wait three minutes before sending another."
        )

    try:
        before = await asyncio.to_thread(client.get_payout_balance)
        balance_before = before["artm_balance"]
    except Exception as exc:
        raise ProviderError(f"Could not read the ImpalaPay balance before the top-up, so nothing was sent: {exc}") from exc

    topup_id = f"TOPUP-{int(now.timestamp() * 1000)}-{secrets.token_hex(2).upper()}"
    doc: dict[str, Any] = {
        "_id": topup_id,
        "merchantId": client.merchant_id or None,  # the float baseline below belongs to THIS account
        "amountKes": amount_kes,
        "payingPhoneMasked": mask_phone(phone),
        "payingPhoneLooksAirtel": looks_like_airtel(phone),
        "commission": commission,
        "expectedFloatKes": expected_float_kes(amount_kes, commission),
        "balanceBefore": balance_before,
        "balanceAfter": None,
        "creditedFloatKes": None,
        "commissionKes": None,
        "status": "PENDING",
        "requestedBy": requested_by,
        "createdAt": now,
        "updatedAt": now,
        "provider": None,
        "error": None,
        "note": None,
        "anomaly": None,
    }

    try:
        response = await asyncio.to_thread(client.topup_via_stk, amount_kes, phone)
    except Exception as exc:
        doc.update(status="FAILED", error=str(exc)[:500])
        await db[COLLECTION].insert_one(doc)
        raise ProviderError(f"ImpalaPay did not accept the top-up: {exc}") from exc

    doc["provider"] = _safe_provider_payload(response)
    await db[COLLECTION].insert_one(doc)

    # Every pending top-up is measured against the same float balance, so a credit from this
    # new push could otherwise also "confirm" an older one that was never approved. Retire the
    # older ones now; they stay in the history as EXPIRED with the reason.
    older = await db[COLLECTION].find({"status": "PENDING"}).to_list(length=50)
    for old in older:
        if old["_id"] != topup_id:
            await db[COLLECTION].update_one(
                {"_id": old["_id"]},
                {"$set": {"status": "EXPIRED", "updatedAt": now,
                          "note": f"Replaced by {topup_id} before any credit was seen."}},
            )
    return doc


async def reconcile_topup(
    db, doc: dict[str, Any], *, client: ImpalaAirtimeClient = impala_airtime, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Confirms a PENDING top-up from the real balance, or marks it expired. Safe to call repeatedly."""
    if doc.get("status") != "PENDING":
        return doc
    now = now or datetime.utcnow()
    update: dict[str, Any] = {}

    # The balance baseline belongs to the reseller account the top-up was made under. After the
    # credentials are rotated, the new account's balance is unrelated, and comparing the two would
    # report a made-up credit. Records from another account (or from before accounts were tracked)
    # are retired instead of settled.
    if (doc.get("merchantId") or None) != (client.merchant_id or None):
        update = {
            "status": "EXPIRED", "updatedAt": now,
            "note": "Created under a different reseller account, so its balance baseline no longer applies. "
                    "Check the Impala dashboard before relying on it.",
        }
        await db[COLLECTION].update_one({"_id": doc["_id"]}, {"$set": update})
        return {**doc, **update}
    try:
        bal = await asyncio.to_thread(client.get_payout_balance)
        now_balance = bal["artm_balance"]
    except Exception:
        return doc  # keep waiting; a failed read must not flip the status

    delta = now_balance - float(doc.get("balanceBefore") or 0.0)
    if delta > 0:
        expected = float(doc.get("expectedFloatKes") or 0)
        update = {
            "status": "CONFIRMED",
            "balanceAfter": now_balance,
            "creditedFloatKes": round(delta, 2),
            "commissionKes": round(delta - float(doc["amountKes"]), 2),
            "updatedAt": now,
        }
        # The float can also change for reasons unrelated to this push (another top-up, airtime sales).
        # A rise far from what the commission predicts must not read like a clean success.
        if expected > 0 and abs(delta - expected) > max(ANOMALY_TOLERANCE_KES, expected * ANOMALY_TOLERANCE_PCT):
            update["anomaly"] = (
                f"The float rose by {delta:,.2f} KES but about {expected:,.2f} KES was expected for this top-up. "
                "Something else may have changed the balance. Check the Impala dashboard before relying on this."
            )
    elif now - doc["createdAt"] > PENDING_WINDOW:
        update = {"status": "EXPIRED", "updatedAt": now}
    if update:
        await db[COLLECTION].update_one({"_id": doc["_id"]}, {"$set": update})
        doc = {**doc, **update}
    return doc


async def cancel_topup(
    db, topup_id: str, *, cancelled_by: Optional[str] = None,
    client: ImpalaAirtimeClient = impala_airtime, now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Stops waiting for a top-up. It only ends OUR tracking: ImpalaPay has no way to recall an STK
    prompt, so if one was delivered and is approved later the float still rises, but nothing here
    will record it. Use it when no prompt reached the phone.

    The balance is checked first. A credit that has already landed settles the record as
    CONFIRMED instead, so a real top-up is never reported as cancelled.

    Raises LookupError for an unknown id and ValueError when the top-up is no longer pending."""
    doc = await db[COLLECTION].find_one({"_id": topup_id})
    if not doc:
        raise LookupError(f"Unknown top-up id: {topup_id}")
    doc = await reconcile_topup(db, doc, client=client, now=now)
    if doc["status"] != "PENDING":
        raise ValueError(f"Top-up {topup_id} is already {doc['status'].lower()}, so there is nothing to cancel.")
    now = now or datetime.utcnow()
    update = {
        "status": "CANCELLED", "updatedAt": now, "cancelledBy": cancelled_by,
        "note": "Cancelled by an operator before any credit was seen.",
    }
    await db[COLLECTION].update_one({"_id": topup_id}, {"$set": update})
    return {**doc, **update}


async def get_topup(db, topup_id: str, *, client: ImpalaAirtimeClient = impala_airtime) -> Optional[dict[str, Any]]:
    doc = await db[COLLECTION].find_one({"_id": topup_id})
    return await reconcile_topup(db, doc, client=client) if doc else None


async def list_topups(db, limit: int = 20, *, client: ImpalaAirtimeClient = impala_airtime) -> list[dict[str, Any]]:
    docs = await db[COLLECTION].find({}).sort("createdAt", -1).limit(limit).to_list(length=limit)
    out = []
    for d in docs:
        out.append(await reconcile_topup(db, d, client=client) if d.get("status") == "PENDING" else d)
    return out
