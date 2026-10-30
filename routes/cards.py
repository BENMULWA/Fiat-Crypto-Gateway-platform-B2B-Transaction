"""
Virtual cards (Deposits -> Virtual Card). The browser talks only to this module; the Mamlaka Partner API key
stays on the server.

  GET  /api/cards/status                    where the user is: KYC, cards, recent operations
  POST /api/cards/kyc                       multipart: `details` (JSON) + front / back / selfie images
  GET  /api/cards/quote?kind=&usd=          what it will cost in KES (provider cost + Jasiri margin)
  POST /api/cards/issue                     issue a card, paid from the KES wallet   (2FA step-up)
  POST /api/cards/{card_no}/fund            load an existing card from the wallet     (2FA step-up)
  POST /api/cards/{card_no}/freeze|unfreeze
  GET  /api/cards/{card_no}/balance
  POST /api/cards/{card_no}/details         full number / expiry / CVV                (2FA step-up)
  GET  /api/cards/operations/{id}           one charge, settled on read
  POST /api/v1/webhooks/mamlaka-cards       signed events from Mamlaka (public)
"""

import hashlib
import hmac
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel

from database import get_db
from routes.auth import get_current_user, get_verified_current_user
from services import card_ops as ops
from services import mamlaka_cards as partner
from two_factor import verify_withdrawal_2fa

logger = logging.getLogger("cards")
router = APIRouter(prefix="/api/cards", tags=["Cards"])
webhook_router = APIRouter(prefix="/api/v1/webhooks", tags=["Cards"])

MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # card processor limit, per image
ALLOWED_UPLOADS = {"image/jpeg": (b"\xff\xd8\xff",), "image/png": (b"\x89PNG\r\n\x1a\n",), "application/pdf": (b"%PDF-",)}


def idempotency(key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=64)) -> str:
    return key


def require_enabled():
    if not ops.enabled():
        raise HTTPException(503, "Virtual cards are not available right now.")


class StepUp(BaseModel):
    """Same step-up as a withdrawal: moving money onto a card, or revealing one, is money-out."""
    otp_session_id: Optional[str] = None
    otp_code: Optional[str] = None
    totp_code: Optional[str] = None


class Charge(StepUp):
    usd: str


class CardRef(StepUp):
    pass


async def _step_up(db, user: dict, body: StepUp):
    await verify_withdrawal_2fa(db, user, body.otp_session_id, body.otp_code, body.totp_code)


def _user_id(user: dict) -> str:
    return str(user["_id"])


# ---------------------------------------------------------------- status
@router.get("/status")
async def card_status(user: dict = Depends(get_current_user), db=Depends(get_db)):
    if not ops.enabled():
        return {"enabled": False}
    user_id = _user_id(user)
    profile = await ops.get_profile(db, user_id)
    if profile:
        profile = await ops.sync_kyc(db, user_id, profile)
    recent = []
    async for op in db[ops.OPS].find({"userId": user_id}).sort("createdAt", -1).limit(10):
        recent.append(ops.public_op(await ops.settle(db, op)))
    cards = []
    if profile and profile.get("kycStatus") == ops.KYC_APPROVED:
        try:
            cards = (await partner.list_cards(user_id)).get("records") or []
        except (partner.MamlakaCardsError, partner.MamlakaCardsUnavailable):
            pass
    low, high = ops.usd_limits()
    return {
        "enabled": True,
        "kyc": {"status": (profile or {}).get("kycStatus") or "none",
                "approved": (profile or {}).get("kycStatus") == ops.KYC_APPROVED,
                "reason": (profile or {}).get("kycReason")},
        "cards": [{"cardNo": c.get("card_no"), "status": c.get("status"), "createdAt": c.get("created_at")} for c in cards],
        "operations": recent,
        "limits": {"minUsd": str(low), "maxUsd": str(high), "marginPct": str(ops.margin_pct())},
        "cardTypeId": ops.card_type_id(),
    }


# ---------------------------------------------------------------- KYC
@router.post("/kyc")
async def submit_card_kyc(details: str = Form(..., description="JSON cardholder details"),
                          front: UploadFile = File(...), selfie: UploadFile = File(...),
                          back: Optional[UploadFile] = File(None), key: str = Depends(idempotency),
                          user: dict = Depends(get_current_user), db=Depends(get_db)):
    require_enabled()
    try:
        body = json.loads(details)
        assert isinstance(body, dict)
    except (ValueError, AssertionError):
        raise HTTPException(422, "details must be a JSON object")
    for sensitive in ("idFrontId", "idBackId", "idHoldId", "merchantOrderNo", "ipAddress"):
        body.pop(sensitive, None)  # set by the server from the uploads

    files = {}
    for kind, upload in (("front", front), ("back", back), ("selfie", selfie)):
        if upload is None:
            continue
        if upload.content_type not in ALLOWED_UPLOADS:
            raise HTTPException(415, f"{kind}: only JPG, PNG and PDF files are supported")
        content = await upload.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"{kind}: file exceeds the 2 MB limit")
        if not any(content.startswith(sig) for sig in ALLOWED_UPLOADS[upload.content_type]):
            raise HTTPException(415, f"{kind}: file content does not match its declared type")
        files[kind] = (upload.filename or f"{kind}.jpg", content, upload.content_type)
    return await ops.submit_kyc(db, user, body, files, key)


# ---------------------------------------------------------------- pricing + charging
@router.get("/quote")
async def card_quote(kind: str = Query(..., pattern="^(issue|fund)$"), usd: str = Query(...),
                     user: dict = Depends(get_current_user), db=Depends(get_db)):
    require_enabled()
    amount = ops.parse_usd(usd)
    pr = await ops.price(ops.ISSUE if kind == "issue" else ops.FUND, amount)
    return {**pr, "kind": kind, "walletAsset": "KES"}


@router.post("/issue")
async def issue_card(body: Charge, key: str = Depends(idempotency),
                     user: dict = Depends(get_verified_current_user), db=Depends(get_db)):
    require_enabled()
    usd = ops.parse_usd(body.usd)
    await _step_up(db, user, body)
    op = await ops.start_operation(db, _user_id(user), ops.ISSUE, usd, key)
    return ops.public_op(await ops.settle(db, op, force=True))


@router.post("/{card_no}/fund")
async def fund_card(card_no: str, body: Charge, key: str = Depends(idempotency),
                    user: dict = Depends(get_verified_current_user), db=Depends(get_db)):
    require_enabled()
    usd = ops.parse_usd(body.usd)
    await _step_up(db, user, body)
    op = await ops.start_operation(db, _user_id(user), ops.FUND, usd, key, card_no=card_no)
    return ops.public_op(await ops.settle(db, op, force=True))


@router.get("/operations/{op_id}")
async def get_operation(op_id: str, user: dict = Depends(get_current_user), db=Depends(get_db)):
    op = await db[ops.OPS].find_one({"_id": op_id, "userId": _user_id(user)})
    if not op:
        raise HTTPException(404, "Operation not found")
    return ops.public_op(await ops.settle(db, op, force=True))


# ---------------------------------------------------------------- card actions
async def _card_call(coro):
    try:
        return await coro
    except partner.MamlakaCardsError as exc:
        raise ops.friendly(exc)
    except partner.MamlakaCardsUnavailable:
        raise HTTPException(503, "Card service is temporarily unavailable. Please try again shortly.")


@router.post("/{card_no}/freeze")
async def freeze_card(card_no: str, key: str = Depends(idempotency), user: dict = Depends(get_current_user)):
    require_enabled()
    return await _card_call(partner.freeze(_user_id(user), ops.op_id_for(_user_id(user), "FREEZE", key), card_no))


@router.post("/{card_no}/unfreeze")
async def unfreeze_card(card_no: str, key: str = Depends(idempotency), user: dict = Depends(get_current_user)):
    require_enabled()
    return await _card_call(partner.unfreeze(_user_id(user), ops.op_id_for(_user_id(user), "UNFREEZE", key), card_no))


@router.get("/{card_no}/balance")
async def balance(card_no: str, user: dict = Depends(get_current_user)):
    require_enabled()
    return await _card_call(partner.card_balance(_user_id(user), card_no))


@router.post("/{card_no}/details")
async def reveal(card_no: str, body: CardRef, response: Response,
                 user: dict = Depends(get_verified_current_user), db=Depends(get_db)):
    """Full card number, expiry and CVV. Step-up required; never stored, never logged, never cached."""
    require_enabled()
    await _step_up(db, user, body)
    response.headers["Cache-Control"] = "no-store"
    logger.warning("card details revealed user=%s card=%s", _user_id(user), card_no)  # who and which, never the values
    return await _card_call(partner.card_details(_user_id(user), card_no))


# ---------------------------------------------------------------- Mamlaka -> Jasiri events
def signature_valid(raw_body: bytes, header: str, secret: str) -> bool:
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.strip().encode(), expected.encode())


@webhook_router.post("/mamlaka-cards")
async def mamlaka_cards_webhook(request: Request, db=Depends(get_db)):
    secret = ops.webhook_secret()
    if not secret:
        raise HTTPException(503, "Card webhook verification is not configured")
    raw = await request.body()
    if not signature_valid(raw, request.headers.get("X-Mamlaka-Signature", ""), secret):
        logger.warning("cards webhook rejected: bad signature")
        raise HTTPException(401, "Invalid signature")
    try:
        event = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    try:
        await ops.handle_event(db, event if isinstance(event, dict) else {})
    except Exception:  # noqa: BLE001 - handlers are idempotent, so a retry by Mamlaka is safe
        logger.exception("cards webhook processing failed")
        raise HTTPException(500, "retry")
    return {"success": True}
