import smtplib
from email.message import EmailMessage

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any
import uuid
import os
import httpx
from routes.ramp import _extract_status_and_success, _has_reconcile_evidence
from broadcast import broadcast_manager
from datetime import datetime, timedelta
from collections import defaultdict
from config import settings
from database import get_db, get_client
from routes.auth import get_current_user_with_role, is_admin_role

try:
    from bson import ObjectId
except ImportError:
    ObjectId = None

router = APIRouter(prefix="/api/admin", tags=["OTC Admin Dashboard"])

def safe_obj_id(val):
    if ObjectId and isinstance(val, str) and len(val) == 24:
        try: return ObjectId(val)
        except: pass
    return val


def ensure_admin(current_user: dict):
    if not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Admin role required")

# 🟢 FIX: Added Pydantic model to correctly catch the JSON body sent by React
class TxStatusUpdate(BaseModel):
    status: str
    provider_report: Optional[Dict[str, Any]] = None


class RiskAlertStatusUpdate(BaseModel):
    status: str


def _normalize_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return None
    return None


def _relative_time(value):
    dt = _normalize_datetime(value)
    if not dt:
        return "Unknown"

    diff = datetime.utcnow() - dt
    minutes = max(int(diff.total_seconds() // 60), 0)
    if minutes < 1:
        return "Just now"
    if minutes < 60:
        return f"{minutes} mins ago"

    hours = minutes // 60
    if hours < 24:
        return f"{hours} hours ago"

    days = hours // 24
    return f"{days} days ago"


def _severity_rank(level: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(str(level).lower(), 0)


def _admin_alert_recipients():
    recipients_raw = getattr(settings, "admin_alert_emails", "") or ""
    return [email.strip().lower() for email in recipients_raw.split(",") if email.strip()]


def _send_admin_risk_email(subject: str, body: str) -> None:
    if not getattr(settings, "smtp_host", ""):
        return

    recipients = _admin_alert_recipients()
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
        pass


async def _get_user_label(db, user_id):
    if not user_id:
        return "Unknown User", None, None
    user = await db["users"].find_one({"_id": safe_obj_id(user_id)}, {"displayName": 1, "name": 1, "email": 1, "kycStatus": 1})
    if not user:
        return "Unknown User", None, None
    return user.get("displayName") or user.get("name") or user.get("email") or "Unknown User", user.get("email"), user.get("kycStatus")


async def _build_aml_flags(db):
    now = datetime.utcnow()
    lookback_24h = now - timedelta(hours=24)
    lookback_6h = now - timedelta(hours=6)
    lookback_1h = now - timedelta(hours=1)

    entries = await db["ramp_entries"].find({"createdAt": {"$gte": lookback_24h}}).sort("createdAt", -1).limit(800).to_list(800)
    metrics_by_user = defaultdict(lambda: {
        "completed_1h": 0,
        "completed_24h": 0,
        "volume_24h": 0.0,
        "failed_6h": 0,
        "last_seen": None,
    })

    for entry in entries:
        created_at = _normalize_datetime(entry.get("createdAt")) or now
        user_key = str(entry.get("userId") or "")
        if not user_key:
            continue

        status_text = str(entry.get("status") or entry.get("transactionStatus") or "").lower()
        amount = 0.0
        try:
            amount = float(entry.get("fromAmount", 0) or 0)
        except Exception:
            amount = 0.0

        user_metrics = metrics_by_user[user_key]
        user_metrics["last_seen"] = max(filter(None, [user_metrics["last_seen"], created_at]), default=created_at)

        if status_text in {"completed", "success", "successful"}:
            user_metrics["completed_24h"] += 1
            user_metrics["volume_24h"] += amount
            if created_at >= lookback_1h:
                user_metrics["completed_1h"] += 1

        if created_at >= lookback_6h and status_text in {"failed", "error", "rejected", "cancelled"}:
            user_metrics["failed_6h"] += 1

    flags = []
    for user_id, metrics in metrics_by_user.items():
        entity, email, kyc_status = await _get_user_label(db, user_id)
        kyc_status = str(kyc_status or "unverified").lower()
        last_seen = metrics["last_seen"] or now

        if metrics["completed_1h"] >= 4:
            severity = "high" if metrics["completed_1h"] >= 6 else "medium"
            flags.append({
                "id": f"velocity-{user_id}",
                "entity": entity,
                "type": "Velocity Check",
                "details": f"{metrics['completed_1h']} completed transactions detected within the last hour.",
                "severity": severity,
                "date": _relative_time(last_seen),
                "createdAt": last_seen.isoformat() + "Z",
                "userId": user_id,
                "userEmail": email,
            })

        if metrics["volume_24h"] >= 250000:
            severity = "high" if metrics["volume_24h"] >= 500000 else "medium"
            flags.append({
                "id": f"volume-{user_id}",
                "entity": entity,
                "type": "Unusual Volume",
                "details": f"24h transaction volume reached KES {metrics['volume_24h']:,.0f}.",
                "severity": severity,
                "date": _relative_time(last_seen),
                "createdAt": last_seen.isoformat() + "Z",
                "userId": user_id,
                "userEmail": email,
            })

        if kyc_status != "verified" and metrics["completed_24h"] > 0:
            flags.append({
                "id": f"kyc-exposure-{user_id}",
                "entity": entity,
                "type": "KYC Exposure",
                "details": f"User has {metrics['completed_24h']} recent transactions while KYC status is {kyc_status}.",
                "severity": "high" if kyc_status == "rejected" else "medium",
                "date": _relative_time(last_seen),
                "createdAt": last_seen.isoformat() + "Z",
                "userId": user_id,
                "userEmail": email,
            })

        if metrics["failed_6h"] >= 3:
            flags.append({
                "id": f"failed-pattern-{user_id}",
                "entity": entity,
                "type": "Failure Pattern",
                "details": f"{metrics['failed_6h']} failed transactions were detected in the last 6 hours.",
                "severity": "medium",
                "date": _relative_time(last_seen),
                "createdAt": last_seen.isoformat() + "Z",
                "userId": user_id,
                "userEmail": email,
            })

    flags.sort(key=lambda item: (_severity_rank(item.get("severity")), item.get("createdAt", "")), reverse=True)
    return flags[:25]


async def _build_risk_alerts(db):
    now = datetime.utcnow()
    alerts = []

    pending_kyc = await db["users"].find({"kycStatus": {"$in": ["pending", "PENDING"]}}).to_list(200)
    old_pending_kyc = []
    for user in pending_kyc:
        submitted_at = _normalize_datetime(user.get("kycSubmittedAt") or user.get("createdAt"))
        if submitted_at and submitted_at <= now - timedelta(hours=12):
            old_pending_kyc.append(user)
    if old_pending_kyc:
        latest = max((_normalize_datetime(user.get("kycSubmittedAt") or user.get("createdAt")) for user in old_pending_kyc), default=now)
        alerts.append({
            "id": "pending-kyc-backlog",
            "message": f"{len(old_pending_kyc)} KYC applications have been pending for more than 12 hours.",
            "severity": "high" if len(old_pending_kyc) >= 5 else "medium",
            "timeAgo": _relative_time(latest),
            "status": "active",
            "category": "compliance",
            "createdAt": latest.isoformat() + "Z" if latest else None,
        })

    recent_failures = await db["ramp_entries"].find({"createdAt": {"$gte": now - timedelta(hours=1)}, "status": {"$in": ["failed", "error", "rejected", "cancelled"]}}).limit(200).to_list(200)
    if recent_failures:
        latest_failure = max((_normalize_datetime(item.get("createdAt")) for item in recent_failures), default=now)
        alerts.append({
            "id": "transaction-failure-spike",
            "message": f"{len(recent_failures)} payment or settlement failures were recorded in the last hour.",
            "severity": "high" if len(recent_failures) >= 5 else "medium",
            "timeAgo": _relative_time(latest_failure),
            "status": "active",
            "category": "operations",
            "createdAt": latest_failure.isoformat() + "Z" if latest_failure else None,
        })

    stuck_entries = await db["ramp_entries"].find({"createdAt": {"$lte": now - timedelta(minutes=30)}, "status": {"$in": ["processing", "pending"]}}).limit(200).to_list(200)
    if stuck_entries:
        latest_stuck = max((_normalize_datetime(item.get("createdAt")) for item in stuck_entries), default=now)
        alerts.append({
            "id": "stuck-processing-transactions",
            "message": f"{len(stuck_entries)} transactions have remained in processing or pending for more than 30 minutes.",
            "severity": "high" if len(stuck_entries) >= 3 else "medium",
            "timeAgo": _relative_time(latest_stuck),
            "status": "active",
            "category": "operations",
            "createdAt": latest_stuck.isoformat() + "Z" if latest_stuck else None,
        })

    liquidity_flags = await db["retail_notifications"].find({"resolved": False, "category": "liquidity"}).limit(500).to_list(500)
    liquidity_by_asset = defaultdict(int)
    latest_liquidity = None
    for item in liquidity_flags:
        asset = item.get("asset")
        if asset:
            liquidity_by_asset[asset] += 1
        item_time = _normalize_datetime(item.get("updatedAt") or item.get("createdAt"))
        if item_time and (latest_liquidity is None or item_time > latest_liquidity):
            latest_liquidity = item_time

    for asset, count in sorted(liquidity_by_asset.items(), key=lambda item: item[1], reverse=True)[:5]:
        if count < 2:
            continue
        alerts.append({
            "id": f"retail-liquidity-{asset}",
            "message": f"{count} retail wallets are below the configured {asset} liquidity threshold.",
            "severity": "high" if count >= 5 else "medium",
            "timeAgo": _relative_time(latest_liquidity or now),
            "status": "active",
            "category": "liquidity",
            "createdAt": (latest_liquidity or now).isoformat() + "Z",
        })

    alert_ids = [alert["id"] for alert in alerts]
    if alert_ids:
        states = await db["admin_risk_alert_states"].find({"alertId": {"$in": alert_ids}}).to_list(len(alert_ids))
        state_map = {item.get("alertId"): item for item in states}
        for alert in alerts:
            state = state_map.get(alert["id"])
            if state and state.get("status"):
                alert["status"] = state.get("status")

    alerts.sort(key=lambda item: (_severity_rank(item.get("severity")), item.get("createdAt", "")), reverse=True)
    return alerts[:20]

@router.get("/operations-overview")
async def get_operations_overview(db=Depends(get_db)):
    # Placeholder for dashboard kpis
    return {"status": "success", "kpis": {}}


@router.get("/company-revenue")
async def get_company_revenue(db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    doc = await db["company_revenue"].find_one({"_id": "corporate_treasury"})
    if not doc:
        return {"status": "success", "revenue": {}}
    # remove Mongo internal id for safety
    doc.pop("_id", None)
    return {"status": "success", "revenue": doc}


class CompanyWithdrawRequest(BaseModel):
    asset: str
    amount: float
    method: str  # 'airtel' or 'internal'
    destination: Optional[Dict[str, Any]] = None


@router.post("/company-withdraw")
async def company_withdraw(body: CompanyWithdrawRequest, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)

    asset = (body.asset or "").strip()
    amount = float(body.amount or 0)
    method = (body.method or "").strip().lower()

    if not asset or amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid asset or amount")

    # Use a MongoDB session/transaction to reserve funds and record the withdrawal atomically
    withdraw_id = f"CW_{uuid.uuid4().hex[:8].upper()}"
    now = datetime.utcnow()

    record = {
        "_id": withdraw_id,
        "asset": asset,
        "amount": amount,
        "method": method,
        "destination": body.destination or {},
        "status": "processing",
        "requestedBy": current_user.get("_id"),
        "createdAt": now,
    }

    client = get_client()
    try:
        async with await client.start_session() as session:
            async with session.start_transaction():
                corp = await db["company_revenue"].find_one({"_id": "corporate_treasury"}, session=session)
                current_bal = float(corp.get(asset, 0)) if corp else 0.0
                if current_bal < amount:
                    raise HTTPException(status_code=400, detail=f"Insufficient company balance for {asset}")

                await db["company_revenue"].update_one({"_id": "corporate_treasury"}, {"$inc": {asset: -amount}}, upsert=True, session=session)
                await db["company_withdrawals"].insert_one(record, session=session)

                # handle internal transfers inside transaction
                if method == "internal":
                    user_id = (body.destination or {}).get("userId")
                    if not user_id:
                        raise HTTPException(status_code=400, detail="destination.userId is required for internal transfers")

                    await db["retail_wallets"].update_one({"userId": safe_obj_id(user_id)}, {"$inc": {asset: amount}}, upsert=True, session=session)
                    await db["company_withdrawals"].update_one({"_id": withdraw_id}, {"$set": {"status": "completed", "completedAt": datetime.utcnow(), "creditedTo": user_id}}, session=session)
                    try:
                        await broadcast_manager.send_user(str(user_id), {"type": "wallet_update", "asset": asset, "amount": amount, "source": "company_withdraw"})
                    except Exception:
                        pass
                    return {"status": "success", "id": withdraw_id, "message": "Internal transfer completed"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # Airtel disburse: performed outside the DB transaction; on network failure we refund.
    if method == "airtel":
        phone = (body.destination or {}).get("phone")
        if not phone:
            # refund reserved funds
            await db["company_revenue"].update_one({"_id": "corporate_treasury"}, {"$inc": {asset: amount}})
            await db["company_withdrawals"].update_one({"_id": withdraw_id}, {"$set": {"status": "failed", "reason": "missing destination.phone"}})
            raise HTTPException(status_code=400, detail="destination.phone is required for airtel disburse")

        phone_s = str(phone).strip().replace(' ', '').replace('-', '')
        if phone_s.startswith('+'):
            phone_s = phone_s[1:]
        if phone_s.startswith('254'):
            phone_s = phone_s[3:]
        if phone_s.startswith('0'):
            phone_s = phone_s[1:]

        gateway_url = os.environ.get("AIRTEL_GATEWAY_URL", "https://airtime.mamlakapsp.com")
        api_key = os.environ.get("AIRTEL_GATEWAY_API_KEY", "")
        disburse_url = f"{gateway_url}/api/v1/disburse"

        payload = {"phone_number": phone_s, "amount": int(amount), "reference": withdraw_id}
        payload["msisdn"] = f"254{phone_s}"
        payload["phone"] = f"0{phone_s}"

        headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient() as client_http:
                resp = await client_http.post(disburse_url, json=payload, headers=headers, timeout=20.0)
                if resp.is_error:
                    raise Exception(f"Gateway HTTP {resp.status_code}: {resp.text}")
                body_resp = resp.json() if resp.content else {}
                success = body_resp.get("success") if isinstance(body_resp, dict) else None
                if success is False:
                    raise Exception(f"Gateway response indicated failure: {body_resp}")

            await db["company_withdrawals"].update_one({"_id": withdraw_id}, {"$set": {"status": "completed", "completedAt": datetime.utcnow(), "gatewayResponse": body_resp}})
            return {"status": "success", "id": withdraw_id, "message": "Airtel disburse initiated"}
        except Exception as exc:
            # refund reserved funds
            await db["company_revenue"].update_one({"_id": "corporate_treasury"}, {"$inc": {asset: amount}})
            await db["company_withdrawals"].update_one({"_id": withdraw_id}, {"$set": {"status": "failed", "reason": str(exc)}})
            raise HTTPException(status_code=502, detail=f"Airtel disburse failed: {str(exc)}")

    # unsupported method: refund
    await db["company_revenue"].update_one({"_id": "corporate_treasury"}, {"$inc": {asset: amount}})
    await db["company_withdrawals"].update_one({"_id": withdraw_id}, {"$set": {"status": "failed", "reason": "unsupported method"}})
    raise HTTPException(status_code=400, detail="Unsupported withdrawal method")


@router.get("/company-withdrawals")
async def list_company_withdrawals(page: int = 1, limit: int = 50, status: str = None, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    try:
        page = max(int(page), 1)
        limit = min(max(int(limit), 1), 200)
    except Exception:
        page = 1
        limit = 50

    query = {}
    if status:
        query["status"] = status

    skip = (page - 1) * limit
    cursor = db["company_withdrawals"].find(query).sort("createdAt", -1).skip(skip).limit(limit)
    items = await cursor.to_list(length=limit)
    total = await db["company_withdrawals"].count_documents(query)

    formatted = []
    for it in items:
        created_at = it.get("createdAt")
        formatted.append({
            "id": str(it.get("_id")),
            "asset": it.get("asset"),
            "amount": it.get("amount"),
            "method": it.get("method"),
            "status": it.get("status"),
            "destination": it.get("destination"),
            "requestedBy": str(it.get("requestedBy")) if it.get("requestedBy") else None,
            "createdAt": created_at.isoformat() + "Z" if isinstance(created_at, datetime) else created_at,
            "completedAt": it.get("completedAt"),
            "reason": it.get("reason"),
        })

    return {"status": "success", "page": page, "limit": limit, "total": total, "items": formatted}

@router.get("/finance/payments")
async def get_admin_finance_payments(db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)

    incoming_cursor = db["ramp_entries"].find({"direction": {"$in": ["on", "in", "incoming"]}}).sort("createdAt", -1).limit(20)
    incoming = await incoming_cursor.to_list(length=20)

    outgoing_cursor = db["ramp_entries"].find({"direction": {"$in": ["off", "out", "outgoing", "swap"]}}).sort("createdAt", -1).limit(20)
    outgoing = await outgoing_cursor.to_list(length=20)

    def fmt_row(entry, mode: str):
        created = entry.get("createdAt") or datetime.utcnow()
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except Exception:
                created = datetime.utcnow()
        status = str(entry.get("status") or "pending").lower()
        if status in {"matched", "completed", "success", "successful"}:
            status_label = "matched"
        elif status in {"failed", "error", "rejected", "cancelled"}:
            status_label = "failed"
        else:
            status_label = "unmatched" if mode == "incoming" else "pending"

        ref = entry.get("reference") or entry.get("transactionRef") or entry.get("externalRef") or str(entry.get("_id"))
        party = entry.get("customerName") or entry.get("userName") or "Unknown Customer"
        amount = entry.get("fromAmount") or entry.get("amount") or 0

        return {
            "id": str(entry.get("_id")),
            "time": created.strftime("%b %d, %Y %H:%M") if isinstance(created, datetime) else str(created),
            "party": party,
            "type": entry.get("channel") or ("on-ramp" if mode == "incoming" else "payout"),
            "amount": f"{float(amount):,.2f}",
            "reference": str(ref),
            "status": status_label,
        }

    incoming_rows = [fmt_row(item, "incoming") for item in incoming]
    outgoing_rows = [fmt_row(item, "outgoing") for item in outgoing]

    unmatched_inbound = sum(1 for row in incoming_rows if row["status"] == "unmatched")
    matched_today = sum(1 for row in incoming_rows if row["status"] == "matched")
    outbound_sent = sum(1 for row in outgoing_rows if row["status"] in {"matched", "completed"})
    outbound_pending = sum(1 for row in outgoing_rows if row["status"] not in {"matched", "completed", "failed"})

    return {
        "status": "success",
        "kpis": {
            "unmatched_inbound": unmatched_inbound,
            "matched_today": matched_today,
            "outbound_sent": outbound_sent,
            "outbound_pending": outbound_pending,
        },
        "incoming": incoming_rows,
        "outgoing": outgoing_rows,
    }


@router.post("/finance/payments/{payment_id}/match")
async def match_admin_payment(payment_id: str, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    entry = await db["ramp_entries"].find_one({"_id": payment_id})
    if not entry:
        try:
            from bson import ObjectId
            entry = await db["ramp_entries"].find_one({"_id": ObjectId(payment_id)})
        except Exception:
            entry = None

    if not entry:
        raise HTTPException(status_code=404, detail="Payment not found")

    await db["ramp_entries"].update_one(
        {"_id": entry.get("_id")},
        {
            "$set": {
                "status": "matched",
                "matchedBy": current_user.get("_id"),
                "matchedAt": datetime.utcnow(),
                "updatedAt": datetime.utcnow(),
            }
        },
    )

    return {"status": "success", "message": "Payment matched successfully"}


@router.get("/dealer/rfqs")
async def get_incoming_rfqs():
    import random
    mock_rfqs = [
        {"id": f"RFQ-{random.randint(1000, 9999)}", "clientName": "Acme Hedge Fund", "channel": "API Integration", "asset": "USDC", "side": "BUY", "size": 250000.00, "status": "pending", "timeAgo": "Just now"}
    ]
    return {"status": "success", "rfqs": mock_rfqs}

@router.get("/analytics/chart-data")
async def get_chart_analytics(days: int = 7, db=Depends(get_db)):
    start_date = datetime.utcnow() - timedelta(days=days)
    pipeline = [
        {"$match": {"createdAt": {"$gte": start_date}, "status": {"$in": ["completed", "COMPLETED", "Completed"]}}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%b %d", "date": "$createdAt"}},
            "volume": {"$sum": "$fromAmount"},
            "revenue": {"$sum": {"$multiply": ["$fromAmount", 0.015]}} 
        }},
        {"$sort": {"_id": 1}}
    ]
    data = await db["ramp_entries"].aggregate(pipeline).to_list(length=days)
    chart_data = [{"date": d["_id"], "volume": round(float(d.get("volume", 0)), 2), "revenue": round(float(d.get("revenue", 0)), 2)} for d in data]
    return {"status": "success", "chartData": chart_data}

@router.get("/retail-transactions")
async def get_all_retail_transactions(userId: str = None, limit: int = 200, db=Depends(get_db)):
    """Fetches all retail transactions, smartly resolving ObjectIds vs Strings"""
    query = {}
    if userId:
        # 🟢 FIX: Search for both String AND ObjectId to guarantee we find the data!
        or_conditions = [{"userId": userId}]
        try:
            from bson import ObjectId
            if len(userId) == 24:
                or_conditions.append({"userId": ObjectId(userId)})
        except:
            pass
        query["$or"] = or_conditions

    cursor = db["ramp_entries"].find(query).sort("createdAt", -1).limit(limit)
    entries = await cursor.to_list(length=limit)

    # Batch user lookup to avoid N+1 queries which can be slow when returning many entries
    user_ids = [e.get("userId") for e in entries if e.get("userId")]
    unique_safe_ids = []
    seen = set()
    for uid in user_ids:
        sid = safe_obj_id(uid)
        key = str(sid)
        if key not in seen:
            seen.add(key)
            unique_safe_ids.append(sid)

    user_map = {}
    if unique_safe_ids:
        users = await db["users"].find({"_id": {"$in": unique_safe_ids}}).to_list(len(unique_safe_ids))
        for u in users:
            user_map[str(u.get("_id"))] = u

    formatted_entries = []
    for e in entries:
        user_id = e.get("userId")
        customer_name = "Unknown User"
        if user_id:
            ukey = str(safe_obj_id(user_id))
            user = user_map.get(ukey)
            if user:
                customer_name = user.get("displayName") or user.get("name") or user.get("email") or "Unknown User"

        formatted_entries.append({
            "id": str(e["_id"]),
            "createdAt": e.get("createdAt", datetime.utcnow()).isoformat() + "Z" if e.get("createdAt") else None,
            "customerName": customer_name,
            "direction": e.get("direction", "swap"),
            "fromAmount": e.get("fromAmount", 0), "fromAsset": e.get("fromAsset", ""),
            "toAmount": e.get("toAmount", 0), "toAsset": e.get("toAsset", ""),
            "status": e.get("status", "pending")
        })

    return {"status": "success", "entries": formatted_entries}

# 🟢 FIX: Use the payload Pydantic model and handle tx_id formats safely
@router.patch("/retail-transactions/{tx_id}/status")
async def moderate_transaction(tx_id: str, payload: TxStatusUpdate, db=Depends(get_db)):
    query = {"_id": tx_id}
    try:
        from bson import ObjectId
        if len(tx_id) == 24:
            query = {"$or": [{"_id": tx_id}, {"_id": ObjectId(tx_id)}]}
    except:
        pass

    entry = await db["ramp_entries"].find_one(query)
    if not entry:
        raise HTTPException(status_code=404, detail="Transaction not found")

    new_status = (payload.status or "").strip().lower()

    # If admin is attempting to mark as completed, ensure provider callback evidence indicates success
    if new_status == "completed":
        # Prefer stored providerReport, allow admin to supply one in the payload for manual reconciliation
        provider_report = entry.get("providerReport") if isinstance(entry.get("providerReport"), dict) else None
        if not provider_report and payload.provider_report:
            provider_report = payload.provider_report

        if not provider_report:
            raise HTTPException(status_code=400, detail="Cannot mark completed: no provider callback/report found. Attach provider_report or wait for provider callback.")

        # Determine provider-reported success/failure
        status_text, success_flag, reason = _extract_status_and_success(provider_report, provider_report.get("transaction") if isinstance(provider_report.get("transaction"), dict) else {})

        if not success_flag:
            raise HTTPException(status_code=400, detail=f"Provider evidence indicates failure: {reason or status_text}")

        # Provider indicates success -> apply wallet credit/refund logic consistent with webhook processing
        try:
            direction = str(entry.get("direction") or "on").lower()
            wallet_asset = entry.get("fromAsset") or "KES"
            amount = float(entry.get("toAmount") or entry.get("fromAmount") or 0)
            user_id = entry.get("userId")

            if direction == "on":
                await db["retail_wallets"].update_one({"userId": user_id}, {"$inc": {wallet_asset: amount}}, upsert=True)

            await db["ramp_entries"].update_one(
                {"_id": entry.get("_id")},
                {
                    "$set": {
                        "status": "completed",
                        "moderatedAt": datetime.utcnow(),
                        "updatedAt": datetime.utcnow(),
                        "providerReport": provider_report,
                        "processedByAdmin": True,
                    }
                }
            )
            try:
                await broadcast_manager.send_user(str(user_id), {
                    "type": "admin_reconciled",
                    "userId": str(user_id),
                    "asset": wallet_asset,
                    "amount": amount,
                    "entryId": str(entry.get("_id")),
                })
            except Exception:
                pass
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to apply wallet update: {str(exc)}")

        return {"status": "success", "message": "Transaction marked completed and wallet updated based on provider evidence."}

    # Non-completion status updates still allowed (e.g., mark as failed)
    result = await db["ramp_entries"].update_one(query, {"$set": {"status": payload.status, "moderatedAt": datetime.utcnow()}})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Transaction not found or no changes applied")
    return {"status": "success"}

@router.get("/compliance/kyc")
async def get_kyc_queue(db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    pending_users = await db["users"].find({"kycStatus": {"$in": ["pending", "PENDING"]}}).sort("createdAt", -1).limit(20).to_list(20)
    
    queue = []
    for u in pending_users:
        kyc_details = u.get("kycDetails", {})
        exact_name = kyc_details.get("fullName") or u.get("fullName") or u.get("displayName") or u.get("name", "Unknown")
        exact_email = kyc_details.get("email") or u.get("email", "Unknown")
        
        submitted_at = u.get("kycSubmittedAt") or u.get("createdAt") or datetime.utcnow()
        if isinstance(submitted_at, str):
            try: submitted_at = datetime.fromisoformat(submitted_at.replace('Z', '+00:00'))
            except: submitted_at = datetime.utcnow()
            
        diff = datetime.utcnow().replace(tzinfo=None) - submitted_at.replace(tzinfo=None)
        hours = int(diff.total_seconds() / 3600)
        mins = int((diff.total_seconds() % 3600) / 60)
        
        if hours > 24: time_ago = f"Submitted {hours // 24} days ago"
        elif hours > 0: time_ago = f"Submitted {hours} hours ago"
        elif mins > 0: time_ago = f"Submitted {mins} mins ago"
        else: time_ago = "Submitted just now"
        
        real_timestamp = submitted_at.strftime("%b %d, %Y, %I:%M %p")
        
        queue.append({
            "id": str(u.get("_id")),
            "name": exact_name,
            "email": exact_email,
            "timeAgo": time_ago,
            "realTimestamp": real_timestamp,
            "submittedAt": submitted_at.isoformat() + "Z" if isinstance(submitted_at, datetime) else None,
            "riskLevel": "low", "kycLevel": "Tier 1",
            "docs": {
                "id": True if kyc_details.get("documentName") or u.get("documentName") else False, 
                "selfie": False, "address": False, "source": False
            }
        })
        
    return {
        "status": "success",
        "kpis": {"pendingKyc": len(queue), "amlFlags": 0, "pepMatches": 0, "sanctions": 0, "riskAlerts": 0},
        "queue": queue
    }


@router.get("/compliance/kyc/{id}")
async def get_kyc_details(id: str, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)

    user = await db["users"].find_one(
        {"_id": safe_obj_id(id)},
        {
            "displayName": 1,
            "name": 1,
            "email": 1,
            "phone": 1,
            "createdAt": 1,
            "kycStatus": 1,
            "kycSubmittedAt": 1,
            "kycReviewedAt": 1,
            "kycReviewedBy": 1,
            "kycReviewNotes": 1,
            "kycDetails": 1,
        },
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    kyc_details = user.get("kycDetails", {})

    return {
        "status": "success",
        "kyc": {
            "id": str(user.get("_id")),
            "name": kyc_details.get("fullName") or user.get("displayName") or user.get("name") or "Unknown",
            "email": kyc_details.get("email") or user.get("email"),
            "phone": kyc_details.get("phone") or user.get("phone"),
            "idNumber": kyc_details.get("idNumber"),
            "kycStatus": user.get("kycStatus", "unverified"),
            "accountCreatedAt": user.get("createdAt"),
            "kycSubmittedAt": user.get("kycSubmittedAt"),
            "kycReviewedAt": user.get("kycReviewedAt"),
            "kycReviewedBy": user.get("kycReviewedBy"),
            "kycReviewNotes": user.get("kycReviewNotes"),
            "document": {
                "name": kyc_details.get("documentName") or user.get("documentName"),
                "mimeType": kyc_details.get("documentMimeType"),
                "size": kyc_details.get("documentSize"),
                "dataUrl": kyc_details.get("documentDataUrl"),
            },
        },
    }


@router.get("/compliance/notifications")
async def get_admin_notifications(db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)

    notifications = await db["admin_notifications"].find({}).sort("createdAt", -1).limit(50).to_list(50)
    unread_count = await db["admin_notifications"].count_documents({"isRead": False})

    formatted = []
    for item in notifications:
        created_at = item.get("createdAt")
        if isinstance(created_at, datetime):
            created_at_iso = created_at.isoformat() + "Z"
            created_at_label = created_at.strftime("%b %d, %Y, %I:%M %p")
        else:
            created_at_iso = str(created_at) if created_at else None
            created_at_label = str(created_at) if created_at else ""

        formatted.append({
            "id": str(item.get("_id")),
            "title": item.get("title", "Notification"),
            "message": item.get("message", ""),
            "type": item.get("type", "info"),
            "category": item.get("category", "general"),
            "route": item.get("route"),
            "isRead": bool(item.get("isRead", False)),
            "createdAt": created_at_iso,
            "createdAtLabel": created_at_label,
            "userId": item.get("userId"),
            "userName": item.get("userName"),
            "userEmail": item.get("userEmail"),
        })

    return {"status": "success", "unreadCount": unread_count, "notifications": formatted}


@router.get("/compliance/monitoring")
async def get_compliance_monitoring(db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)

    aml_flags = await _build_aml_flags(db)
    risk_alerts = await _build_risk_alerts(db)

    return {
        "status": "success",
        "kpis": {
            "amlFlags": len(aml_flags),
            "pepMatches": 0,
            "sanctions": 0,
            "riskAlerts": len(risk_alerts),
            "highRiskAlerts": len([alert for alert in risk_alerts if alert.get("severity") == "high"]),
        },
        "amlFlags": aml_flags,
        "riskAlerts": risk_alerts,
    }


@router.post("/compliance/risk-alerts/{alert_id}/status")
async def update_risk_alert_status(alert_id: str, payload: RiskAlertStatusUpdate, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    normalized_status = str(payload.status or "").strip().lower()
    if normalized_status not in {"active", "investigating", "acknowledged", "escalated"}:
        raise HTTPException(status_code=400, detail="Unsupported risk alert status")

    risk_alerts = await _build_risk_alerts(db)
    target_alert = next((alert for alert in risk_alerts if alert.get("id") == alert_id), None)
    if not target_alert:
        raise HTTPException(status_code=404, detail="Risk alert not found")

    now = datetime.utcnow()
    admin_name = current_user.get("displayName") or current_user.get("name") or current_user.get("email") or "Admin"

    await db["admin_risk_alert_states"].update_one(
        {"alertId": alert_id},
        {
            "$set": {
                "alertId": alert_id,
                "status": normalized_status,
                "updatedAt": now,
                "updatedBy": current_user.get("_id"),
            },
            "$setOnInsert": {"createdAt": now},
        },
        upsert=True,
    )

    if normalized_status == "acknowledged":
        await db["admin_risk_alert_states"].update_one(
            {"alertId": alert_id},
            {"$set": {"acknowledgedAt": now, "acknowledgedBy": current_user.get("_id")}},
        )

    if normalized_status == "escalated":
        await db["admin_risk_alert_states"].update_one(
            {"alertId": alert_id},
            {"$set": {"escalatedAt": now, "escalatedBy": current_user.get("_id")}},
        )

        notification_doc = {
            "category": "risk_escalation",
            "type": "risk_alert_escalated",
            "title": f"Escalated risk alert: {target_alert.get('category', 'risk').title()}",
            "message": f"{admin_name} escalated alert '{target_alert.get('message', 'Risk alert')}'.",
            "route": "/kyc-aml",
            "isRead": False,
            "sourceAlertId": alert_id,
            "severity": target_alert.get("severity"),
            "createdAt": now,
        }
        await db["admin_notifications"].update_one(
            {"sourceAlertId": alert_id, "type": "risk_alert_escalated"},
            {"$set": notification_doc, "$setOnInsert": {"createdAt": now}},
            upsert=True,
        )

        _send_admin_risk_email(
            subject=f"Escalated Risk Alert: {target_alert.get('category', 'risk').title()}",
            body=(
                f"A risk alert has been escalated on the Mamlaka admin console.\n\n"
                f"Escalated By: {admin_name}\n"
                f"Alert ID: {alert_id}\n"
                f"Category: {target_alert.get('category', 'risk')}\n"
                f"Severity: {target_alert.get('severity', 'unknown')}\n"
                f"Details: {target_alert.get('message', 'No details')}\n"
                f"Time: {now.isoformat()}Z\n"
            ),
        )

    return {"status": "success"}

# Notifications for marking all admin notifications as read, and it requires admin role to access.

@router.post("/compliance/notifications/mark-all-read")
async def mark_all_admin_notifications_read(db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    await db["admin_notifications"].update_many({"isRead": False}, {"$set": {"isRead": True, "readAt": datetime.utcnow(), "readBy": current_user.get("_id")}})
    return {"status": "success"}

@router.post("/compliance/kyc/{id}/approve")
async def approve_kyc(id: str, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    res = await db["users"].update_one(
        {"_id": safe_obj_id(id)},
        {
            "$set": {
                "kycStatus": "verified",
                "kycReviewedAt": datetime.utcnow(),
                "kycReviewedBy": current_user.get("_id"),
                "kycReviewNotes": "Approved by admin",
            }
        },
    )
    if res.modified_count == 0: raise HTTPException(404, "User not found")
    return {"status": "approved"}

@router.post("/compliance/kyc/{id}/reject")
async def reject_kyc(id: str, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    res = await db["users"].update_one(
        {"_id": safe_obj_id(id)},
        {
            "$set": {
                "kycStatus": "rejected",
                "kycReviewedAt": datetime.utcnow(),
                "kycReviewedBy": current_user.get("_id"),
                "kycReviewNotes": "Rejected by admin",
            }
        },
    )
    if res.modified_count == 0: raise HTTPException(404, "User not found")
    return {"status": "rejected"}

@router.get("/finance/customers")
async def get_customers_list(db=Depends(get_db)):
    users = await db["users"].find().sort("createdAt", -1).limit(100).to_list(100)
    customers = []
    for u in users:
        wallet = await db["retail_wallets"].find_one({"userId": u["_id"]})
        total_vol = sum(float(v) for k, v in (wallet or {}).items() if k not in ["_id", "userId"])
        
        kyc_details = u.get("kycDetails", {})
        exact_name = kyc_details.get("fullName") or u.get("fullName") or u.get("displayName") or u.get("name", "Unknown")
        
        created_at = u.get("createdAt")
        joined_at_iso = created_at.isoformat() + "Z" if isinstance(created_at, datetime) else str(created_at) if created_at else None
        
        kyc_date = u.get("kycSubmittedAt", created_at)
        kyc_date_iso = kyc_date.isoformat() + "Z" if isinstance(kyc_date, datetime) else str(kyc_date) if kyc_date else None
        
        customers.append({
            "id": str(u.get("_id")),
            "name": exact_name,
            "legalName": exact_name,
            "email": kyc_details.get("email") or u.get("email", "Unknown"),
            "phone": kyc_details.get("phone") or u.get("phone", "N/A"),
            "idNumber": kyc_details.get("idNumber") or u.get("idNumber", "N/A"),
            "documentName": kyc_details.get("documentName") or u.get("documentName", "None provided"),
            "kycSubmittedAt": kyc_date_iso,
            "joinedAt": joined_at_iso,
            "kyc": u.get("kycStatus", "unverified").lower(),
            "risk": "low" if total_vol < 100000 else "high",
            "volume": f"KES {total_vol:,.0f}",
            "status": u.get("accountStatus", u.get("status", "active")),
        })
    return {"status": "success", "customers": customers}

@router.post("/compliance/customers/{id}/freeze")
async def freeze_customer(id: str, db=Depends(get_db)):
    res = await db["users"].update_one({"_id": safe_obj_id(id)}, {"$set": {"accountStatus": "frozen", "status": "frozen"}})
    return {"status": "frozen"}

@router.post("/compliance/customers/{id}/unfreeze")
async def unfreeze_customer(id: str, db=Depends(get_db)):
    res = await db["users"].update_one({"_id": safe_obj_id(id)}, {"$set": {"accountStatus": "active", "status": "active"}})
    return {"status": "active"}