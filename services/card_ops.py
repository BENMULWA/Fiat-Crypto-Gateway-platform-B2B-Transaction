"""
Virtual cards as a Jasiri funding channel: KYC, issuance, loading from the KES wallet, and settlement.

Money rules
  * The user is charged Mamlaka's quoted cost plus Jasiri's margin (CARD_MARGIN_PCT), so a rate change on the
    partner side can never leave Jasiri out of pocket. Amounts are Decimal; only the final wallet call uses float
    because retail_wallets stores floats.
  * Debit FIRST (wallet_utils.debit_wallet), then call the partner with Idempotency-Key = operation id.
  * A definitive refusal (4xx) refunds ONCE, using the same once-only marker the ramp callbacks use.
  * An unknown outcome (timeout / 5xx) NEVER refunds: the money stays held and the operation is settled later by
    reading it back (webhook, status poll, or replaying the same Idempotency-Key).
  * Anything ambiguous goes to REVIEW for a human. Nothing here auto-fails money it cannot account for.

card_operations: one document per charge, _id = deterministic id from (user, kind, client idempotency key).
"""

import hashlib
import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_UP
from typing import Any, Optional

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from broadcast import broadcast_manager
from services import mamlaka_cards as partner

logger = logging.getLogger("cards")

OPS, PROFILES, EVENTS = "card_operations", "card_profiles", "card_events"
ISSUE, FUND = "ISSUE", "FUND"
PENDING_DEBIT, DEBITED, SUBMITTED, SUCCESS, REFUNDED, FAILED, REVIEW = (
    "PENDING_DEBIT", "DEBITED", "SUBMITTED", "SUCCESS", "REFUNDED", "FAILED", "REVIEW")
OPEN = (DEBITED, SUBMITTED)                      # money is held and the outcome is not final
STUCK_AFTER = timedelta(hours=24)
CRASH_AFTER = timedelta(minutes=5)               # a debit that never confirmed: ambiguous, a human checks
POLL_EVERY = timedelta(seconds=15)
KYC_POLL_EVERY = timedelta(seconds=60)
KYC_APPROVED = "pass_audit"


# ---------------------------------------------------------------- config
def enabled() -> bool:
    return os.environ.get("CARD_FEATURE_ENABLED", "true").strip().lower() == "true" and partner.configured()


def card_type_id() -> int:
    return int(os.environ.get("CARD_TYPE_ID", "111078") or 111078)


def margin_pct() -> Decimal:
    return Decimal(os.environ.get("CARD_MARGIN_PCT", "2.0") or "2.0")


def usd_limits() -> tuple[Decimal, Decimal]:
    return (Decimal(os.environ.get("CARD_MIN_USD", "10") or "10"), Decimal(os.environ.get("CARD_MAX_USD", "100") or "100"))


def webhook_secret() -> str:
    return os.environ.get("MAMLAKA_CARDS_WEBHOOK_SECRET", "").strip()


def parse_usd(value: Any) -> Decimal:
    """USD amount without going through float; at most 2 decimals; inside the configured limits."""
    try:
        usd = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HTTPException(422, "Enter a valid USD amount.")
    if not usd.is_finite() or usd != usd.quantize(Decimal("0.01")):
        raise HTTPException(422, "USD amounts support at most 2 decimal places.")
    low, high = usd_limits()
    if usd < low or usd > high:
        raise HTTPException(422, f"Amount must be between {low} and {high} USD.")
    return usd.quantize(Decimal("0.01"))


def op_id_for(user_id: str, kind: str, idempotency_key: str) -> str:
    return "CARD_" + hashlib.sha256(f"{user_id}:{kind}:{idempotency_key}".encode()).hexdigest()[:24].upper()


def public_op(op: dict) -> dict:
    return {"id": op["_id"], "kind": op["kind"], "usd": op["usd"], "chargedKes": op["chargeKes"],
            "status": op["status"], "cardNo": op.get("cardNo"), "reason": op.get("reason"),
            "createdAt": op["createdAt"].isoformat() + "Z"}


async def notify(user_id: str, reason: str, **extra):
    """Live update for the user's open Deposit page (same WebSocket the deposits already use)."""
    try:
        await broadcast_manager.send_user(str(user_id), {"type": "card_update", "reason": reason, **extra})
    except Exception:  # noqa: BLE001 - a dropped push must never fail a money operation
        logger.warning("card_update push failed for user %s", user_id)


def friendly(exc: "partner.MamlakaCardsError") -> HTTPException:
    """Never leak the partner's internals (float balance, limits) to the end user."""
    if exc.status in (409, 422):
        return HTTPException(422 if exc.status == 422 else 409, exc.message)
    logger.error("card provider refused (HTTP %s): %s", exc.status, exc.message)
    return HTTPException(502, "Card service is temporarily unavailable. Your wallet was not charged.")


# ---------------------------------------------------------------- pricing
async def price(kind: str, usd: Decimal) -> dict:
    try:
        q = await partner.quote("issue" if kind == ISSUE else "deposit", str(usd), card_type_id())
    except partner.MamlakaCardsError as exc:
        raise friendly(exc)
    except partner.MamlakaCardsUnavailable:
        raise HTTPException(503, "Card service is temporarily unavailable. Please try again shortly.")
    cost = Decimal(q["kes"])
    charge = (cost * (Decimal(1) + margin_pct() / Decimal(100))).quantize(Decimal("0.01"), rounding=ROUND_UP)
    return {"usd": str(usd), "costKes": str(cost), "chargeKes": str(charge), "marginKes": str(charge - cost),
            "cardFeeUsd": q.get("cardFeeUsd")}


# ---------------------------------------------------------------- KYC
async def get_profile(db, user_id: str) -> Optional[dict]:
    return await db[PROFILES].find_one({"_id": str(user_id)})


async def submit_kyc(db, user: dict, details: dict, files: dict, idempotency_key: str) -> dict:
    """files: {"front": (name, bytes, type), "back": ..., "selfie": ...}. One call from the form: we create the
    customer, upload the images, then submit; Wasabi/the issuing bank decide asynchronously."""
    user_id = str(user["_id"])
    profile = await get_profile(db, user_id)
    if profile and profile.get("kycStatus") in ("wait_audit", "under_review", KYC_APPROVED):
        raise HTTPException(409, "Your card application is already submitted.")
    resubmit = bool(profile and profile.get("kycStatus") == "reject" and profile.get("holderId"))

    first, last = details.get("firstName", "").strip(), details.get("lastName", "").strip()
    try:
        await partner.ensure_customer(user_id, f"{first} {last}".strip() or "Jasiri customer",
                                      details.get("email") or user.get("email", ""),
                                      f"{details.get('areaCode', '')}{details.get('mobile', '')}")
        ids = {}
        for kind, field in (("front", "idFrontId"), ("back", "idBackId"), ("selfie", "idHoldId")):
            if kind in files:
                name, content, ctype = files[kind]
                ids[field] = (await partner.upload_document(user_id, kind, name, content, ctype))["fileId"]
        payload = {**details, **ids}
        payload.setdefault("cardTypeId", card_type_id())
        if resubmit:
            result = await partner.resubmit_kyc(user_id, int(profile["holderId"]), idempotency_key, payload)
        else:
            result = await partner.submit_kyc(user_id, idempotency_key, payload)
    except partner.MamlakaCardsError as exc:
        raise HTTPException(422 if exc.status in (409, 422) else 502, exc.message if exc.status in (409, 422)
                            else "Card service is temporarily unavailable. Please try again shortly.")
    except partner.MamlakaCardsUnavailable:
        raise HTTPException(503, "Card service is temporarily unavailable. Please try again shortly.")

    data = result.get("data") or {}
    holder_id = data.get("holderId") or (profile or {}).get("holderId")
    now = datetime.utcnow()
    await db[PROFILES].update_one(
        {"_id": user_id},
        {"$set": {"holderId": holder_id, "kycStatus": data.get("status") or "wait_audit", "kycReason": None,
                  "cardTypeId": payload["cardTypeId"], "holderName": f"{first} {last}".strip(),
                  "updatedAt": now, "kycCheckedAt": now},
         "$setOnInsert": {"createdAt": now}},
        upsert=True)
    await notify(user_id, "kyc_submitted")
    return {"holderId": holder_id, "status": data.get("status") or "wait_audit"}


async def sync_kyc(db, user_id: str, profile: dict, force: bool = False) -> dict:
    """Pull the decision from Mamlaka while it is pending (throttled: each pull reaches the card processor)."""
    if profile.get("kycStatus") not in ("wait_audit", "under_review"):
        return profile
    last = profile.get("kycCheckedAt")
    if not force and last and datetime.utcnow() - last < KYC_POLL_EVERY:
        return profile
    try:
        records = (await partner.kyc_status(user_id, refresh=True)).get("records") or []
    except (partner.MamlakaCardsError, partner.MamlakaCardsUnavailable):
        return profile
    holder = next((r for r in records if r.get("holder_id") == profile.get("holderId")), records[0] if records else None)
    update = {"kycCheckedAt": datetime.utcnow()}
    if holder:
        update.update(kycStatus=holder.get("status"), kycReason=holder.get("description") if holder.get("status") == "reject" else None)
    await db[PROFILES].update_one({"_id": user_id}, {"$set": update})
    if holder and holder.get("status") != profile.get("kycStatus"):
        await notify(user_id, "kyc_status_changed", status=holder.get("status"))
    return {**profile, **update}


# ---------------------------------------------------------------- charging
async def _call_partner(op: dict) -> dict:
    """Issue / deposit with the operation id as the idempotency key (so a replay reads the same operation)."""
    req = op["request"]
    if op["kind"] == ISSUE:
        return await partner.issue_card(op["userId"], op["_id"], req["cardTypeId"], req["holderId"], op["usd"])
    return await partner.deposit(op["userId"], op["_id"], req["cardNo"], op["usd"])


async def _set(db, op_id: str, **fields):
    await db[OPS].update_one({"_id": op_id}, {"$set": {**fields, "updatedAt": datetime.utcnow()}})


async def refund(db, op: dict, reason: str) -> dict:
    """Give the held KES back exactly once. The marker lives on the wallet row, so a duplicate webhook, a replayed
    settle or a retried request cannot refund twice."""
    from routes.ramp import _apply_wallet_delta_once  # lazy: routes.ramp is a heavy import

    await _apply_wallet_delta_once(db, op["userId"], "KES", float(Decimal(op["chargeKes"])), op["_id"], "appliedCardRefunds")
    await db[OPS].update_one({"_id": op["_id"], "status": {"$in": [PENDING_DEBIT, DEBITED, SUBMITTED]}},
                             {"$set": {"status": REFUNDED, "reason": reason, "updatedAt": datetime.utcnow()}})
    await notify(op["userId"], "card_operation_refunded", operation=op["_id"])
    return await db[OPS].find_one({"_id": op["_id"]})


async def apply_result(db, op: dict, state: str, reference: Optional[str] = None, card_no: Optional[str] = None) -> dict:
    """Move an open operation to its final state. SUCCESS keeps the money spent; FAILED refunds once."""
    if op["status"] not in OPEN:
        return op
    if reference and not op.get("partnerRef"):
        await _set(db, op["_id"], partnerRef=reference)
    if state == "SUCCESS":
        fields = {"status": SUCCESS, "reason": None}
        if card_no:
            fields["cardNo"] = card_no
        await db[OPS].update_one({"_id": op["_id"], "status": {"$in": list(OPEN)}},
                                 {"$set": {**fields, "updatedAt": datetime.utcnow()}})
        await notify(op["userId"], "card_operation_succeeded", operation=op["_id"], kind=op["kind"])
        return await db[OPS].find_one({"_id": op["_id"]})
    if state == "FAILED":
        return await refund(db, op, "The card provider could not complete the operation.")
    return op  # still pending


async def start_operation(db, user_id: str, kind: str, usd: Decimal, idempotency_key: str,
                          card_no: Optional[str] = None) -> dict:
    user_id = str(user_id)
    op_id = op_id_for(user_id, kind, idempotency_key)
    existing = await db[OPS].find_one({"_id": op_id})
    if existing:  # a retried request never charges or sends twice
        return existing

    request: dict = {}
    if kind == ISSUE:
        profile = await get_profile(db, user_id)
        if not profile or profile.get("kycStatus") != KYC_APPROVED or not profile.get("holderId"):
            raise HTTPException(409, "Your card application has not been approved yet.")
        request = {"cardTypeId": int(profile.get("cardTypeId") or card_type_id()), "holderId": int(profile["holderId"])}
    else:
        if not card_no:
            raise HTTPException(422, "Choose a card to load.")
        request = {"cardNo": card_no}

    pr = await price(kind, usd)
    now = datetime.utcnow()
    op = {"_id": op_id, "userId": user_id, "kind": kind, "usd": str(usd), "costKes": pr["costKes"],
          "chargeKes": pr["chargeKes"], "marginKes": pr["marginKes"], "status": PENDING_DEBIT,
          "request": request, "cardNo": card_no, "partnerRef": None, "createdAt": now, "updatedAt": now}
    try:
        await db[OPS].insert_one(op)
    except DuplicateKeyError:
        return await db[OPS].find_one({"_id": op_id})

    from wallet_utils import debit_wallet  # lazy: wallet_utils and routes/ import each other

    try:
        await debit_wallet(db, user_id, "KES", float(Decimal(pr["chargeKes"])))
    except HTTPException as exc:
        await _set(db, op_id, status=FAILED, reason="insufficient wallet balance")
        raise HTTPException(402, f"Insufficient KES balance. This costs {pr['chargeKes']} KES.") if exc.status_code == 400 else exc
    await _set(db, op_id, status=DEBITED)
    op["status"] = DEBITED

    try:
        res = await _call_partner(op)
    except partner.MamlakaCardsError as exc:
        await refund(db, op, f"Provider refused: {exc.message}")
        raise friendly(exc)
    except partner.MamlakaCardsUnavailable:
        logger.warning("card op %s outcome unknown - funds held, will settle", op_id)
        await _set(db, op_id, note="outcome unknown")
        return await db[OPS].find_one({"_id": op_id})

    reference = res.get("merchantOrderNo")
    card = res.get("cardNo") or (res.get("data") or {}).get("cardNo")
    await _set(db, op_id, status=SUBMITTED, partnerRef=reference, cardNo=card or card_no)
    op = await db[OPS].find_one({"_id": op_id})
    return await apply_result(db, op, res.get("status", "PENDING"), reference, card)


async def holder_name(db, user_id: str, profile: Optional[dict]) -> Optional[str]:
    """The name printed on the card: as submitted for KYC (it matches the ID). Profiles from before it was
    stored are filled once from the name Mamlaka holds for the customer."""
    if not profile:
        return None
    if profile.get("holderName"):
        return profile["holderName"]
    try:
        name = ((await partner.get_customer(user_id)) or {}).get("fullName")
    except (partner.MamlakaCardsError, partner.MamlakaCardsUnavailable):
        return None
    if name:
        await db[PROFILES].update_one({"_id": user_id}, {"$set": {"holderName": name}})
    return name


async def settle(db, op: dict, force: bool = False) -> dict:
    """Read the outcome back from the partner. Safe to call repeatedly (webhook, status poll, reconcile)."""
    now = datetime.utcnow()
    if op["status"] == PENDING_DEBIT and now - op["createdAt"] > CRASH_AFTER:
        # The debit may or may not have been applied before the crash: never guess with money.
        await _set(db, op["_id"], status=REVIEW, reason="debit not confirmed - check the wallet manually")
        logger.error("card op %s stuck before debit confirmation - REVIEW", op["_id"])
        return await db[OPS].find_one({"_id": op["_id"]})
    if op["status"] not in OPEN:
        return op
    if not force and op.get("lastCheckedAt") and now - op["lastCheckedAt"] < POLL_EVERY:
        return op
    await _set(db, op["_id"], lastCheckedAt=now)

    try:
        if op.get("partnerRef"):
            res = await partner.operation(op["partnerRef"], refresh=True)
            state, reference, card = res.get("status"), op["partnerRef"], None
        else:  # the original call never answered: replaying the same key reads (or safely creates) the operation
            res = await _call_partner(op)
            state, reference = res.get("status"), res.get("merchantOrderNo")
            card = res.get("cardNo") or (res.get("data") or {}).get("cardNo")
    except partner.MamlakaCardsUnavailable:
        return op
    except partner.MamlakaCardsError as exc:
        logger.error("card op %s settle refused (HTTP %s) - REVIEW", op["_id"], exc.status)
        await _set(db, op["_id"], status=REVIEW, reason="provider could not confirm - check manually")
        return await db[OPS].find_one({"_id": op["_id"]})

    if state in ("PENDING", "UNKNOWN", "INITIATED"):
        if now - op["createdAt"] > STUCK_AFTER:
            await _set(db, op["_id"], status=REVIEW, reason="still pending after 24h - check manually")
            return await db[OPS].find_one({"_id": op["_id"]})
        if reference and not op.get("partnerRef"):
            await _set(db, op["_id"], partnerRef=reference, status=SUBMITTED)
        return op
    return await apply_result(db, op, state, reference, card)


async def settle_open_operations(db) -> int:
    """Reconcile job / safety net: every operation still holding money gets read back."""
    count = 0
    async for op in db[OPS].find({"status": {"$in": [PENDING_DEBIT, *OPEN]}}):
        await settle(db, op, force=True)
        count += 1
    return count


# ---------------------------------------------------------------- webhook from Mamlaka
async def handle_event(db, event: dict) -> str:
    """Apply one signed event. Idempotent: a repeat of an event that was fully processed is a no-op."""
    event_id = event.get("id")
    if not event_id:
        return "ignored"
    try:
        await db[EVENTS].insert_one({"_id": event_id, "type": event.get("type"), "state": "received",
                                     "receivedAt": datetime.utcnow()})
    except DuplicateKeyError:
        prior = await db[EVENTS].find_one({"_id": event_id})
        if prior and prior.get("state") == "processed":
            return "duplicate"

    data, user_id = event.get("data") or {}, str((event.get("data") or {}).get("customerId") or "")
    kind = event.get("type")
    if kind == "kyc.status_changed" and user_id:
        update = {"kycStatus": data.get("status"), "kycReason": data.get("reason"), "updatedAt": datetime.utcnow()}
        await db[PROFILES].update_one({"_id": user_id}, {"$set": update})
        await notify(user_id, "kyc_status_changed", status=data.get("status"))
    elif kind == "operation.updated":
        op = await db[OPS].find_one({"partnerRef": data.get("reference")})
        if op:
            await apply_result(db, op, data.get("status"), data.get("reference"))
    elif kind == "card.updated" and user_id:
        await notify(user_id, "card_changed", cardNo=data.get("cardNo"))
    await db[EVENTS].update_one({"_id": event_id}, {"$set": {"state": "processed"}})
    return "processed"
