"""
Merchant-facing OTC endpoints -- the self-service counterpart to the
admin-initiated dealer desk in routes/otc_admin.py. Institutional customers
sign up (routes/auth.py::signup_verify_otp with account_type="institutional"),
complete onboarding here, fund their institutional_wallets balance, and
create/negotiate/accept their own RFQs -- which flow into the *same*
dealer_rfqs/dealer_settlements pipeline and treasury action state machine
already built for the admin-initiated path. Nothing about quoting,
screening, or settlement is duplicated here; only the entry point is new.
"""
import uuid
from datetime import datetime

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from database import get_db
from routes.auth import get_current_user, is_admin_role
from routes.otc_admin import (
    _fetch_dealer_rfq,
    _serialize_dealer_rfq,
    _accept_dealer_rfq_core,
    AnalysisEngine,
)
from broadcast import broadcast_manager
from notifications import notify_user
from institutional_wallet_utils import get_institutional_wallet, lock_funds

router = APIRouter(prefix="/api/otc", tags=["OTC Merchant Portal"])


# --- Institutional-approval gate -------------------------------------------
# Mirrors routes/auth.py::get_verified_current_user's pattern exactly (an
# opt-in stricter dependency layered on top of get_current_user, not a
# change to it) but checks institutional onboarding approval instead of
# retail KYC -- the two are different gates for different account types and
# must not be conflated onto the same kycStatus field.
async def get_verified_institutional_user(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    try:
        user_id = ObjectId(str(current_user.get("_id")))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid user identity.") from exc

    user_doc = await db.users.find_one({"_id": user_id}, {"role": 1, "businessName": 1})
    if not user_doc:
        raise HTTPException(status_code=401, detail="User session is no longer valid.")
    role = user_doc.get("role", "retail")
    if role != "institutional":
        raise HTTPException(status_code=403, detail="This account is not an institutional OTC account.")

    profile = await db["institutional_profiles"].find_one({"userId": str(user_id)})
    onboarding_status = str((profile or {}).get("onboardingStatus", "not_started"))
    if onboarding_status != "approved":
        raise HTTPException(
            status_code=403,
            detail=f"Onboarding must be approved before using OTC settlement (current status: {onboarding_status}).",
        )

    return {**current_user, "role": role, "businessName": user_doc.get("businessName"), "onboardingStatus": onboarding_status}


async def _get_own_profile(db, user_id: str) -> dict:
    profile = await db["institutional_profiles"].find_one({"userId": user_id})
    if not profile:
        raise HTTPException(status_code=404, detail="Institutional profile not found")
    return profile


# --- Onboarding --------------------------------------------------------------

@router.get("/onboarding")
async def get_onboarding_status(db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    profile = await db["institutional_profiles"].find_one({"userId": str(current_user["_id"])})
    if not profile:
        raise HTTPException(status_code=404, detail="Institutional profile not found -- sign up as an institutional account first")
    profile.pop("_id", None)
    return {"status": "success", "profile": profile}


@router.put("/onboarding/business-overview")
async def update_business_overview(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    user_id = str(current_user["_id"])
    await _get_own_profile(db, user_id)
    fields = {k: payload.get(k) for k in (
        "legalName", "companyType", "businessModel", "incorporationNumber",
        "dateOfIncorporation", "countryOfIncorporation", "taxNumber",
        "companyAddress", "zipCode", "state", "city", "businessDescription", "companyWebsite",
    ) if k in payload}
    fields["updatedAt"] = datetime.utcnow()
    await db["institutional_profiles"].update_one({"userId": user_id}, {"$set": fields})
    return {"status": "success"}


async def _update_onboarding_list(db, user_id: str, field: str, items: list):
    await _get_own_profile(db, user_id)
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail=f"{field} must be a list")
    await db["institutional_profiles"].update_one(
        {"userId": user_id},
        {"$set": {field: items, "updatedAt": datetime.utcnow()}},
    )
    return {"status": "success"}


@router.put("/onboarding/directors")
async def update_directors(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    return await _update_onboarding_list(db, str(current_user["_id"]), "directors", payload.get("directors", []))


@router.put("/onboarding/shareholders")
async def update_shareholders(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    return await _update_onboarding_list(db, str(current_user["_id"]), "shareholders", payload.get("shareholders", []))


@router.put("/onboarding/peps")
async def update_peps(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    return await _update_onboarding_list(db, str(current_user["_id"]), "peps", payload.get("peps", []))


@router.put("/onboarding/documents")
async def update_documents(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    """
    Records document metadata (name/type/reference) against the profile.
    Actual file storage should reuse whatever mechanism routes/retail.py's
    /kyc/submit already uses for retail KYC documents rather than a second
    upload pipeline -- wire that in here once confirmed, this endpoint just
    persists the resulting references.
    """
    return await _update_onboarding_list(db, str(current_user["_id"]), "documents", payload.get("documents", []))


@router.post("/onboarding/submit")
async def submit_onboarding(db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    user_id = str(current_user["_id"])
    profile = await _get_own_profile(db, user_id)
    missing = [f for f in ("directors", "shareholders", "documents") if not profile.get(f)]
    if not profile.get("legalName") and not profile.get("businessName"):
        missing.append("legalName")
    if missing:
        raise HTTPException(status_code=400, detail=f"Cannot submit onboarding -- missing: {', '.join(missing)}")

    now = datetime.utcnow()
    await db["institutional_profiles"].update_one(
        {"userId": user_id},
        {"$set": {"onboardingStatus": "under_review", "submittedAt": now, "updatedAt": now}},
    )
    await db["admin_notifications"].insert_one({
        "category": "institutional_onboarding",
        "type": "onboarding_submitted",
        "title": f"Institutional onboarding submitted: {profile.get('businessName', user_id)}",
        "message": "Review directors, shareholders, PEPs and documents, then approve or reject.",
        "route": "/admin/compliance",
        "isRead": False,
        "sourceUserId": user_id,
        "createdAt": now,
    })
    return {"status": "success", "onboardingStatus": "under_review"}


# --- Wallet (read-only from the merchant side; crediting is a treasury/admin action) ---

@router.get("/wallet")
async def get_my_wallet(db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    wallet = await get_institutional_wallet(db, str(current_user["_id"]))
    wallet.pop("_id", None)
    return {"status": "success", "balances": wallet}


# --- Merchant-initiated RFQs -------------------------------------------------

@router.post("/rfqs")
async def create_own_rfq(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_verified_institutional_user)):
    amount = float(payload.get("amount", 0) or 0)
    from_asset = str(payload.get("from_asset", "")).strip().upper()
    to_asset = str(payload.get("to_asset", "")).strip().upper()
    if amount <= 0 or not from_asset or not to_asset:
        raise HTTPException(status_code=400, detail="Valid amount, sell asset, and buy asset are required")

    rfq_id = f"RFQ-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.utcnow()
    rfq = {
        "id": rfq_id,
        "rfq_display": rfq_id,
        "customerId": str(current_user["_id"]),
        "customerName": current_user.get("businessName") or str(current_user["_id"]),
        "fromAsset": from_asset,
        "toAsset": to_asset,
        "side": str(payload.get("side", "SELL")).upper(),
        "amount": amount,
        "settlementChannel": payload.get("settlement_channel", "WALLET_TO_BANK"),
        "collectionPhone": payload.get("collection_phone"),
        "destinationWallet": payload.get("destination_wallet"),
        "network": payload.get("network"),
        "channel": "DEALER",
        # Distinguishes merchant self-service RFQs from admin-initiated ones
        # in the same dealer_rfqs collection -- everything downstream
        # (quote/accept/execute/settle) treats them identically.
        "origin": "merchant_self_service",
        "status": "quote_ready",
        "createdAt": now,
        "updatedAt": now,
    }
    rfq["analysis"] = await AnalysisEngine(db).analyze(rfq)
    rfq["status"] = "quote_ready" if rfq["analysis"]["passed"] else "blocked"
    await db["dealer_rfqs"].insert_one(rfq)

    await db["admin_notifications"].insert_one({
        "category": "dealer_rfq",
        "type": "rfq_submitted_by_merchant",
        "title": f"New merchant RFQ: {rfq_id}",
        "message": f"{rfq['customerName']} requested {amount:,.2f} {from_asset} -> {to_asset}.",
        "route": "/admin/institutional-rfqs",
        "isRead": False,
        "sourceRfqId": rfq_id,
        "createdAt": now,
    })

    return {"status": "success", "rfq": _serialize_dealer_rfq(rfq)}


@router.get("/rfqs")
async def list_own_rfqs(db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    rfqs = await db["dealer_rfqs"].find({"customerId": str(current_user["_id"])}).sort("createdAt", -1).to_list(length=200)
    return {"status": "success", "rfqs": [_serialize_dealer_rfq(r) for r in rfqs]}


async def _get_own_rfq_or_404(db, rfq_id: str, current_user: dict) -> dict:
    rfq = await _fetch_dealer_rfq(db, rfq_id)
    if not rfq or str(rfq.get("customerId")) != str(current_user["_id"]):
        raise HTTPException(status_code=404, detail="RFQ not found")
    return rfq


@router.get("/rfqs/{rfq_id}")
async def get_own_rfq(rfq_id: str, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    rfq = await _get_own_rfq_or_404(db, rfq_id, current_user)
    return {"status": "success", "rfq": _serialize_dealer_rfq(rfq)}


@router.post("/rfqs/{rfq_id}/accept")
async def merchant_accept_rfq(rfq_id: str, db=Depends(get_db), current_user: dict = Depends(get_verified_institutional_user)):
    rfq = await _get_own_rfq_or_404(db, rfq_id, current_user)

    async def _lock_merchant_funds(db, rfq: dict, execution: dict) -> None:
        # Locks what the merchant is committing (the from-leg) -- released
        # back to available if the settlement later fails/is cancelled
        # (routes/otc_admin.py's action endpoint already calls
        # TreasuryPositionEngine.release_route/spend_route for the platform
        # side; this is the mirror on the merchant's own ledger).
        locked = await lock_funds(db, str(current_user["_id"]), rfq.get("fromAsset"), float(rfq.get("amount", 0) or 0))
        if not locked:
            raise HTTPException(status_code=409, detail="Insufficient available balance to accept this trade")

    result = await _accept_dealer_rfq_core(db, rfq_id, current_user, extra_on_success=_lock_merchant_funds)
    return result


# --- Chat -- shared by both the merchant portal and the dealer's existing --
# --- RFQ workspace (DealerWorkspaceLive.tsx); access is either the RFQ's ---
# --- own customer, or any admin/dealer. -------------------------------------

async def _assert_rfq_participant(db, rfq_id: str, current_user: dict) -> dict:
    rfq = await _fetch_dealer_rfq(db, rfq_id)
    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")
    is_owner = str(rfq.get("customerId")) == str(current_user.get("_id"))
    if not is_owner and not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Not a participant on this RFQ")
    return rfq


@router.get("/rfqs/{rfq_id}/messages")
async def get_rfq_messages(rfq_id: str, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    await _assert_rfq_participant(db, rfq_id, current_user)
    messages = await db["dealer_rfq_messages"].find({"rfqId": rfq_id}).sort("createdAt", 1).to_list(length=500)
    for m in messages:
        m.pop("_id", None)
    return {"status": "success", "messages": messages}


@router.post("/rfqs/{rfq_id}/messages")
async def post_rfq_message(rfq_id: str, payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    rfq = await _assert_rfq_participant(db, rfq_id, current_user)
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 4000:
        raise HTTPException(status_code=400, detail="Message too long")

    is_sender_admin = is_admin_role(current_user.get("role"))
    now = datetime.utcnow()
    message = {
        "id": f"MSG-{uuid.uuid4().hex[:10].upper()}",
        "rfqId": rfq_id,
        "fromUserId": str(current_user.get("_id")),
        "fromRole": "dealer" if is_sender_admin else "merchant",
        "text": text,
        "createdAt": now,
    }
    await db["dealer_rfq_messages"].insert_one(dict(message))

    # Deliver live to whichever side didn't send it. broadcast_manager is an
    # in-memory, single-process pub/sub (see broadcast.py's own docstring) --
    # fine for the current single-instance deployment, would need a real
    # pub/sub (Redis) before running multiple backend instances.
    recipient_id = rfq.get("customerId") if is_sender_admin else None
    if recipient_id:
        await broadcast_manager.send_user(str(recipient_id), {"type": "rfq_message", "rfqId": rfq_id, "message": message})
        try:
            await notify_user(
                db, recipient_id, "dealer_rfq", "info",
                "New message from your dealer",
                text[:140],
                extra={"rfqId": rfq_id},
            )
        except Exception:
            pass
    if not is_sender_admin:
        # Notify admin desk generally -- individual dealer WS delivery would
        # need a stable admin-user routing convention this codebase doesn't
        # have yet (admin_notifications is the existing broad channel).
        await db["admin_notifications"].insert_one({
            "category": "dealer_rfq",
            "type": "rfq_message_from_merchant",
            "title": f"New message on {rfq_id}",
            "message": text[:200],
            "route": "/admin/institutional-rfqs",
            "isRead": False,
            "sourceRfqId": rfq_id,
            "createdAt": now,
        })

    return {"status": "success", "message": message}
