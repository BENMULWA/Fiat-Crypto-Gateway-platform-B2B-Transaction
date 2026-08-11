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

router = APIRouter(prefix="/api/retail", tags=["Retail User"])

# ALL SUPPORTED ASSETS IN THE PLATFORM
SUPPORTED_ASSETS = [
    "KES", "USDA", "USDT", "USDC", "USD", 
    "UGX", "TZS", "RWF", "BIF", "XAF", "XOF", 
    "AIRT", "IMP", "BTC", "ETH"
]


#NotificatioN TO USER BALANCE IS BELOW THRESHOLD ACTION NEEDS TO BE ADDED
RETAIL_NOTIFICATION_THRESHOLDS = {
    "KES": 100,
    "USDA": 10,
    "USDT": 10,
    "USDC": 10,
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
        await db["retail_notifications"].update_one(
            {"userId": user_id, "code": code},
            {
                "$set": {
                    "userId": user_id,
                    "code": code,
                    "category": alert["category"],
                    "severity": alert["severity"],
                    "title": alert["title"],
                    "message": alert["message"],
                    "asset": alert["asset"],
                    "balance": alert["balance"],
                    "threshold": alert["threshold"],
                    "isRead": False,
                    "resolved": False,
                    "updatedAt": now,
                },
                "$setOnInsert": {
                    "createdAt": now,
                },
            },
            upsert=True,
        )

    if active_codes:
        await db["retail_notifications"].update_many(
            {"userId": user_id, "code": {"$nin": active_codes}, "resolved": False},
            {"$set": {"resolved": True, "resolvedAt": now, "updatedAt": now}},
        )
    else:
        await db["retail_notifications"].update_many(
            {"userId": user_id, "resolved": False},
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
async def update_retail_profile(profile: ProfileUpdate):
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

    return {
        "status": "success",
        "kycStatus": user.get("kycStatus", "unverified"),
        "kycDetails": user.get("kycDetails", {}),
        "kycSubmittedAt": user.get("kycSubmittedAt"),
        "kycReviewedAt": user.get("kycReviewedAt"),
        "kycReviewNotes": user.get("kycReviewNotes"),
    }