import smtplib
from email.message import EmailMessage

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from datetime import datetime
from config import settings

# Safe MongoDB ObjectId converter
try:
    from bson import ObjectId
except ImportError:
    ObjectId = None

def safe_object_id(val):
    """Safely converts string IDs to MongoDB ObjectIds if necessary."""
    if ObjectId and isinstance(val, str) and len(val) == 24:
        try:
            return ObjectId(val)
        except:
            pass
    return val


def build_user_id_candidates(val):
    """Return all likely userId representations (string/ObjectId)."""
    candidates = []

    if val is None:
        return candidates

    candidates.append(val)
    val_str = str(val)
    if val_str not in candidates:
        candidates.append(val_str)

    oid = safe_object_id(val_str)
    if oid not in candidates:
        candidates.append(oid)

    return candidates


def _send_admin_kyc_email(subject: str, body: str) -> None:
    if not getattr(settings, "smtp_host", ""):
        return

    recipients_raw = getattr(settings, "admin_alert_emails", "") or ""
    recipients = [email.strip().lower() for email in recipients_raw.split(",") if email.strip()]
    if not recipients:
        return

    sender = getattr(settings, "smtp_from_email", "") or getattr(settings, "smtp_user", "")
    if not sender:
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    try:
        server = smtplib.SMTP(getattr(settings, "smtp_host", ""), int(getattr(settings, "smtp_port", 587) or 587), timeout=20)
        if bool(getattr(settings, "smtp_use_tls", True)):
            server.starttls()
        smtp_user = getattr(settings, "smtp_user", "")
        if smtp_user:
            server.login(smtp_user, getattr(settings, "smtp_password", ""))
        server.send_message(msg)
        server.quit()
    except Exception:
        # Email alerts are best-effort; keep KYC submission working even if SMTP fails.
        pass

# Import shared JWT config and auth helper
from routes.auth import get_current_user
from database import get_db
from typing import Optional
import uuid

router = APIRouter(prefix="/api/retail", tags=["Retail User"])

# ALL SUPPORTED ASSETS IN THE PLATFORM
SUPPORTED_ASSETS = [
    "KES", "USDA", "USDT", "USDC", "cUSD", "USD", 
    "UGX", "TZS", "RWF", "BIF", "XAF", "XOF", 
    "AIRT", "IMP", "BTC", "ETH"
]


#NotificatioN TO USER BALANCE IS BELOW THRESHOLD ACTION NEEDS TO BE ADDED
RETAIL_NOTIFICATION_THRESHOLDS = {
    "KES": 100,
    "USDA": 10,
    "USDT": 10,
    "USDC": 10,
    "cUSD": 10,
    "USD": 50,
    "UGX": 100000,
    "TZS": 100000,
    "RWF": 50000,
    "BIF": 100000,
    "XAF": 50000,
    "XOF": 50000,
    "AIRT": 100,
    "IMP": 100,
    "BTC": 0.001,
    "ETH": 0.02,
}


def _format_asset_amount(asset: str, amount: float) -> str:
    if asset in {"BTC", "ETH"}:
        return f"{amount:.6f}"
    if amount >= 1000:
        return f"{amount:,.0f}"
    if amount >= 1:
        return f"{amount:,.2f}"
    return f"{amount:.4f}"


def _build_retail_alerts(wallet: dict | None):
    alerts = []
    if not wallet:
        return alerts

    for asset, threshold in RETAIL_NOTIFICATION_THRESHOLDS.items():
        balance = float(wallet.get(asset, 0.0) or 0.0)
        if balance <= 0:
            alerts.append({
                "code": f"{asset}_EMPTY",
                "category": "liquidity",
                "severity": "high",
                "title": f"{asset} balance is empty",
                "message": f"Your {asset} wallet is empty. Add funds before placing new trades or withdrawals.",
                "asset": asset,
                "balance": balance,
                "threshold": threshold,
            })
        elif balance < threshold:
            alerts.append({
                "code": f"{asset}_LOW",
                "category": "liquidity",
                "severity": "medium",
                "title": f"{asset} balance is below threshold",
                "message": f"Your {asset} balance is {_format_asset_amount(asset, balance)} and below the {_format_asset_amount(asset, threshold)} threshold.",
                "asset": asset,
                "balance": balance,
                "threshold": threshold,
            })

    return alerts


async def _sync_retail_notifications(db, user_id, wallet):
    alerts = _build_retail_alerts(wallet)
    now = datetime.utcnow()

    if wallet is None:
        return alerts

    active_codes = []
    for alert in alerts:
        code = alert["code"]
        active_codes.append(code)

        # Whether to force isRead back to False must NOT be unconditional --
        # this resync runs on every GET /notifications call (every 30s poll,
        # plus every time the bell dropdown reopens), and a threshold breach
        # that's still active a poll later would otherwise get re-marked
        # unread right after the user hits "Mark all as read", making that
        # button look broken (it worked; the very next poll just undid it).
        # Only reset to unread for a genuinely new alert (no existing doc)
        # or a fresh re-trigger of one the user had already cleared
        # (previously resolved=True) -- an alert that's been continuously
        # active and already acknowledged stays read.
        existing = await db["retail_notifications"].find_one(
            {"userId": user_id, "code": code}, {"resolved": 1}
        )
        set_fields = {
            "userId": user_id,
            "code": code,
            "category": alert["category"],
            "severity": alert["severity"],
            "title": alert["title"],
            "message": alert["message"],
            "asset": alert["asset"],
            "balance": alert["balance"],
            "threshold": alert["threshold"],
            "resolved": False,
            "updatedAt": now,
        }
        if not existing or existing.get("resolved"):
            set_fields["isRead"] = False

        await db["retail_notifications"].update_one(
            {"userId": user_id, "code": code},
            {
                "$set": set_fields,
                "$setOnInsert": {"createdAt": now},
            },
            upsert=True,
        )

    # Scoped to category "liquidity" only — this resync must not sweep up and
    # silently resolve the one-off event notifications from notify_user()
    # (deposit/withdrawal/swap/KYC events), which aren't threshold-based and
    # have nothing to do with the current wallet snapshot.
    if active_codes:
        await db["retail_notifications"].update_many(
            {"userId": user_id, "category": "liquidity", "code": {"$nin": active_codes}, "resolved": False},
            {"$set": {"resolved": True, "resolvedAt": now, "updatedAt": now}},
        )
    else:
        await db["retail_notifications"].update_many(
            {"userId": user_id, "category": "liquidity", "resolved": False},
            {"$set": {"resolved": True, "resolvedAt": now, "updatedAt": now}},
        )

    return alerts

@router.get("/wallet")
async def get_retail_wallet_balances(db=Depends(get_db), current_user=Depends(get_current_user)):
    user_ids = build_user_id_candidates(current_user.get("_id"))
    wallets = await db["retail_wallets"].find({"userId": {"$in": user_ids}}).to_list(length=100)
    
    # Sum balances across all matching wallet rows (legacy/object-id migration safe).
    balances = {}
    for asset in SUPPORTED_ASSETS:
        total = 0.0
        for wallet in wallets:
            try:
                total += float(wallet.get(asset, 0.0) or 0.0)
            except Exception:
                continue
        balances[asset] = total

    return {"status": "success", "balances": balances}


@router.get("/notifications")
async def get_retail_notifications(db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = safe_object_id(current_user.get("_id"))
    wallet = await db["retail_wallets"].find_one({"userId": {"$in": build_user_id_candidates(current_user.get("_id"))}})
    alerts = await _sync_retail_notifications(db, user_id, wallet)

    notifications = await db["retail_notifications"].find({"userId": user_id}).sort("updatedAt", -1).limit(50).to_list(50)
    unread_count = await db["retail_notifications"].count_documents({"userId": user_id, "isRead": False, "resolved": False})

    formatted = []
    for item in notifications:
        created_at = item.get("createdAt")
        updated_at = item.get("updatedAt") or created_at
        timestamp = updated_at or created_at
        if isinstance(timestamp, datetime):
            created_at_iso = timestamp.isoformat() + "Z"
            created_at_label = timestamp.strftime("%b %d, %Y, %I:%M %p")
        else:
            created_at_iso = str(timestamp) if timestamp else None
            created_at_label = str(timestamp) if timestamp else ""

        formatted.append({
            "id": str(item.get("_id")),
            "title": item.get("title", "Notification"),
            "message": item.get("message", ""),
            "type": item.get("severity", "info"),
            "category": item.get("category", "general"),
            "asset": item.get("asset"),
            "balance": item.get("balance"),
            "threshold": item.get("threshold"),
            "route": item.get("route") or "/wallets",
            "isRead": bool(item.get("isRead", False)),
            "resolved": bool(item.get("resolved", False)),
            "createdAt": created_at_iso,
            "createdAtLabel": created_at_label,
        })

    if not formatted and alerts:
        for alert in alerts:
            formatted.append({
                "id": alert["code"],
                "title": alert["title"],
                "message": alert["message"],
                "type": alert["severity"],
                "category": alert["category"],
                "asset": alert["asset"],
                "balance": alert["balance"],
                "threshold": alert["threshold"],
                "route": "/wallets",
                "isRead": False,
                "resolved": False,
                "createdAt": None,
                "createdAtLabel": "Just now",
            })

    return {"status": "success", "unreadCount": unread_count, "notifications": formatted}


@router.post("/notifications/mark-all-read")
async def mark_all_retail_notifications_read(db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = safe_object_id(current_user.get("_id"))
    await db["retail_notifications"].update_many(
        {"userId": user_id, "isRead": False, "resolved": False},
        {"$set": {"isRead": True, "readAt": datetime.utcnow(), "readBy": user_id}},
    )
    return {"status": "success"}

class ProfileUpdate(BaseModel):
    name: str
    email: str
    phone: str

@router.put("/profile")
async def update_retail_profile(profile: ProfileUpdate, db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = safe_object_id(current_user.get("_id"))

    # get_current_user only decodes the JWT (id + workspace) and doesn't carry
    # the persisted email, so it has to be read from the user doc here to
    # compare against what was submitted.
    user_doc = await db["users"].find_one({"_id": user_id}, {"email": 1})
    if not user_doc:
        raise HTTPException(status_code=401, detail="User session is no longer valid.")

    name = profile.name.strip()
    phone = profile.phone.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Full name cannot be empty.")

    # Login is email/OTP-based (see auth.py's admin-only change-email recovery
    # flow), so a plain profile field can't be allowed to silently repoint it —
    # that bypasses the collision check and re-verification that flow enforces.
    # Only name/phone are safe to self-serve here.
    current_email = (user_doc.get("email") or "").strip().lower()
    if profile.email.strip().lower() != current_email:
        raise HTTPException(status_code=400, detail="Email cannot be changed here. Contact support to update your login email.")

    await db["users"].update_one(
        {"_id": user_id},
        {"$set": {"name": name, "phone": phone}},
    )
    return {"status": "success", "message": "Profile updated successfully"}

class KycSubmission(BaseModel):
    fullName: str
    idNumber: str
    email: str
    phone: str
    documentName: str | None = None
    documentDataUrl: str | None = None
    documentMimeType: str | None = None
    documentSize: int | None = None

@router.post("/kyc/submit")
async def submit_kyc(payload: KycSubmission, db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = safe_object_id(current_user.get("_id"))

    document_data_url = (payload.documentDataUrl or "").strip()
    if document_data_url and not document_data_url.startswith("data:"):
        raise HTTPException(status_code=400, detail="Invalid KYC document format.")

    if payload.documentSize is not None and int(payload.documentSize) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="KYC document exceeds 10MB limit.")

    now = datetime.utcnow()

    await db["users"].update_one(
        {"_id": user_id},
        {"$set": {
            "kycStatus": "pending",
            "kycSubmittedAt": now,
            "kycReviewedAt": None,
            "kycReviewedBy": None,
            "kycReviewNotes": None,
            "kycDetails": {
                "fullName": payload.fullName,
                "idNumber": payload.idNumber,
                "email": payload.email,
                "phone": payload.phone,
                "documentName": payload.documentName or "identity-document",
                "documentMimeType": payload.documentMimeType,
                "documentSize": payload.documentSize,
                "documentDataUrl": document_data_url or None,
            }
        }},
        upsert=False
    )

    notification_doc = {
        "category": "kyc",
        "type": "new_submission",
        "title": "New KYC submission",
        "message": f"{payload.fullName} submitted KYC and is awaiting review.",
        "userId": str(user_id),
        "userName": payload.fullName,
        "userEmail": payload.email,
        "kycSubmittedAt": now,
        "route": "/kyc-aml",
        "isRead": False,
        "createdAt": now,
    }

    try:
        await db["admin_notifications"].insert_one(notification_doc)
    except Exception:
        pass

    _send_admin_kyc_email(
        subject=f"New KYC submission: {payload.fullName} ({payload.email})",
        body=(
            f"A new KYC submission is waiting for review.\n\n"
            f"Name: {payload.fullName}\n"
            f"Email: {payload.email}\n"
            f"Phone: {payload.phone}\n"
            f"ID/Passport: {payload.idNumber}\n"
            f"Alert Type: KYC Review Pending\n"
            f"Submitted At: {now.isoformat()}Z\n"
        ),
    )

    return {"status": "success", "message": "KYC submitted and pending admin review.", "kycStatus": "pending", "submittedAt": now.isoformat() + "Z"}

@router.get("/kyc/status")
async def get_kyc_status(db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = safe_object_id(current_user.get("_id"))
    user = await db["users"].find_one({"_id": user_id}, {"kycStatus": 1, "kycDetails": 1, "kycSubmittedAt": 1, "kycReviewedAt": 1, "kycReviewNotes": 1})
    
    if not user:
        return {"status": "success", "kycStatus": "unverified", "kycDetails": {}}

    # Older accounts may have been created with pending before any KYC details
    # existed. Treat them as unverified so they can submit the form.
    kyc_status = user.get("kycStatus", "unverified")
    if kyc_status == "pending" and not user.get("kycSubmittedAt") and not user.get("kycDetails"):
        kyc_status = "unverified"

    return {
        "status": "success",
        "kycStatus": kyc_status,
        "kycDetails": user.get("kycDetails", {}),
        "kycSubmittedAt": user.get("kycSubmittedAt"),
        "kycReviewedAt": user.get("kycReviewedAt"),
        "kycReviewNotes": user.get("kycReviewNotes"),
    }


class InternalTransferRequest(BaseModel):
    recipient_email: str
    asset: str
    amount: float
    otp_session_id: str
    otp_code: str
    totp_code: str
    note: Optional[str] = None


def _mask_display_name(name: str) -> str:
    """'John Kariuki' -> 'John K.' — same partial-name confirmation banks
    show before you commit to an internal transfer (e.g. KCB/M-Pesa's
    "confirm recipient" step), without exposing the full name to a sender
    who only knows the recipient's email."""
    parts = [p for p in (name or "").strip().split(" ") if p]
    if not parts:
        return "Jasiri User"
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[1][0]}."


# Recipient verification step before a transfer is confirmed — mirrors the
# "confirm recipient name" screen on a bank's own-bank transfer flow. Returns
# only a masked display name, never the full name/phone, so a sender can't
# use this to enumerate account holder details from a bare email guess.
@router.get("/transfer/lookup")
async def lookup_transfer_recipient(
    email: str,
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    recipient_email = (email or "").strip().lower()
    if not recipient_email:
        raise HTTPException(status_code=400, detail="Email is required.")

    sender_email = (current_user.get("email") or "").strip().lower()
    if recipient_email == sender_email:
        return {"status": "success", "found": False, "isSelf": True}

    recipient_doc = await db["users"].find_one({"email": recipient_email})
    if not recipient_doc:
        return {"status": "success", "found": False, "isSelf": False}

    display_name = recipient_doc.get("displayName") or recipient_doc.get("name") or ""
    return {
        "status": "success",
        "found": True,
        "isSelf": False,
        "displayName": _mask_display_name(display_name),
        "kycVerified": recipient_doc.get("kycStatus") == "verified",
    }


# Off-chain, instant, zero-fee balance transfer between two Jasiri accounts —
# no blockchain transaction, just an internal ledger move. Gated behind the
# same email-OTP + mandatory-TOTP check as a real withdrawal (see
# two_factor.verify_withdrawal_2fa) because it moves funds out of an account
# just as irreversibly from the sender's point of view, even though nothing
# touches a chain.
@router.post("/transfer")
async def transfer_to_user(
    payload: InternalTransferRequest,
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    # Imported locally, not at module load: wallet_utils imports
    # build_user_id_candidates from this module, so importing wallet_utils at
    # the top of routes/retail.py would be a circular import.
    from wallet_utils import debit_wallet, credit_wallet
    from two_factor import verify_withdrawal_2fa

    asset = payload.asset.strip().upper()
    if asset not in SUPPORTED_ASSETS:
        raise HTTPException(status_code=400, detail=f"Unsupported asset: {asset}")
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Transfer amount must be greater than zero.")

    recipient_email = (payload.recipient_email or "").strip().lower()
    if not recipient_email:
        raise HTTPException(status_code=400, detail="Recipient email is required.")

    sender_email = (current_user.get("email") or "").strip().lower()
    if recipient_email == sender_email:
        raise HTTPException(status_code=400, detail="You can't transfer to your own account.")

    recipient_doc = await db["users"].find_one({"email": recipient_email})
    if not recipient_doc:
        raise HTTPException(status_code=404, detail="No Jasiri account found with that email.")

    # Verifies email OTP + mandatory TOTP; raises before any balance moves.
    await verify_withdrawal_2fa(db, current_user, payload.otp_session_id, payload.otp_code, payload.totp_code)

    sender_id = current_user.get("_id")
    recipient_id = recipient_doc["_id"]

    await debit_wallet(db, sender_id, asset, payload.amount)
    try:
        await credit_wallet(db, recipient_id, asset, payload.amount)
    except Exception as exc:
        # Credit failed after the debit succeeded — reverse it immediately so
        # the sender's funds aren't stranded in limbo.
        await credit_wallet(db, sender_id, asset, payload.amount)
        raise HTTPException(status_code=502, detail=f"Transfer failed and was reversed: {exc}")

    now = datetime.utcnow()
    transfer_id = str(uuid.uuid4())
    await db["internal_transfers"].insert_one({
        "_id": transfer_id,
        "asset": asset,
        "amount": payload.amount,
        "senderId": sender_id,
        "senderEmail": sender_email,
        "recipientId": recipient_id,
        "recipientEmail": recipient_email,
        "note": (payload.note or "").strip()[:280],
        "createdAt": now,
    })

    return {
        "status": "success",
        "id": transfer_id,
        "asset": asset,
        "amount": payload.amount,
        "recipientEmail": recipient_email,
        "message": f"Sent {payload.amount} {asset} to {recipient_email}.",
    }


@router.get("/transfers")
async def get_transfer_history(db=Depends(get_db), current_user=Depends(get_current_user)):
    """Sent and received internal transfers for the logged-in user, newest first."""
    email = (current_user.get("email") or "").strip().lower()
    records = await db["internal_transfers"].find(
        {"$or": [{"senderEmail": email}, {"recipientEmail": email}]}
    ).sort("createdAt", -1).limit(100).to_list(100)

    transfers = []
    for r in records:
        transfers.append({
            "id": r.get("_id"),
            "asset": r.get("asset"),
            "amount": r.get("amount"),
            "direction": "sent" if r.get("senderEmail") == email else "received",
            "counterparty": r.get("recipientEmail") if r.get("senderEmail") == email else r.get("senderEmail"),
            "note": r.get("note", ""),
            "createdAt": r.get("createdAt").isoformat() + "Z" if r.get("createdAt") else None,
        })

    return {"status": "success", "transfers": transfers}
