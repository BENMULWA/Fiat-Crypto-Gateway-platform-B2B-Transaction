import smtplib
from email.message import EmailMessage

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any
import uuid
import os
import httpx
from routes.ramp import _extract_status_and_success, _has_reconcile_evidence, _apply_wallet_delta_once, build_user_id_candidates, resolve_momo_provider_and_validate, _resolve_tx_explorer
from routes.treasury import DEFAULT_USD_BASE_RATES
from services.zigram_client import ZigramClient, ZigramError, is_clear_status, resolve_screening_leg
from broadcast import broadcast_manager
from notifications import notify_user
from datetime import datetime, timedelta
from collections import defaultdict
from config import settings
from database import get_db, get_client
from dealer_engine.analysis import AnalysisEngine
from dealer_engine.positions import TreasuryPositionEngine
from routes.auth import get_current_user_with_role, is_admin_role

try:
    from bson import ObjectId
except ImportError:
    ObjectId = None

router = APIRouter(prefix="/api/admin", tags=["OTC Admin Dashboard"])
zigram = ZigramClient()


async def _screen_dealer_rfq(db, *, rfq: dict, quote: dict, current_user: dict, rate_book: dict) -> bool:
    """
    Screens an institutional RFQ with ZIGRAM before any liquidity is reserved
    or settlement legs are created. Dealer/OTC tickets are the highest AML
    priority on this platform -- bulky institutional amounts, not retail
    pocket change -- so this runs at accept time, before ANY commitment,
    rather than only at execute time.

    Fail-closed: any ZIGRAM error or non-clear Monitoring_Status holds the RFQ
    instead of proceeding. See ZigramClient's docstring for why "clear"
    statuses are never guessed, and _screen_ramp_transaction in routes/ramp.py
    for the same pattern on the retail side.
    """
    rates = dict(DEFAULT_USD_BASE_RATES)
    rates.update(rate_book.get("usd_base_rates", {}))
    currency, amount = resolve_screening_leg(
        rfq.get("fromAsset"), rfq.get("toAsset"),
        rfq.get("amount"), quote.get("receive_amount"),
        usd_base_rates=rates,
    )

    customer_id = str(rfq.get("customerId") or rfq.get("customerName") or "UNKNOWN")
    check_doc = {
        "rfq_id": rfq.get("id"),
        "customer_id": customer_id,
        "amount": amount,
        "currency": currency,
        "created_at": datetime.utcnow(),
        "screened_by": current_user.get("_id"),
    }

    # DEMO ONLY (OTC_DEMO_SKIP_ZIGRAM=true): real screening below is untouched;
    # this just short-circuits it and leaves an audit record saying so.
    if settings.otc_demo_skip_zigram:
        check_doc.update({"outcome": "demo_skipped"})
        await db["compliance_checks"].insert_one(check_doc)
        return True

    try:
        result = zigram.submit_transaction(
            customer_id=customer_id,
            transaction_id=rfq.get("id"),
            amount=amount,
            currency=currency,
            mode="Dealer Desk",
            transaction_type="OTC Settlement",
            transaction_status="Pending",
            channel=rfq.get("settlementChannel", "DEALER"),
        )
    except ZigramError as e:
        check_doc.update({"outcome": "error", "error": str(e)})
        await db["compliance_checks"].insert_one(check_doc)
        return False

    check_doc.update({
        "outcome": "screened",
        "is_success": result["is_success"],
        "monitoring_status": result["monitoring_status"],
        "case_display_id": result["case_display_id"],
        "master_case_display_id": result["master_case_display_id"],
        "raw_response": result["raw"],
    })
    await db["compliance_checks"].insert_one(check_doc)

    if not result["is_success"]:
        return False
    return is_clear_status(result["monitoring_status"])

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
async def get_operations_overview(days: int = 7, scope: str = "retail", db=Depends(get_db)):
    now = datetime.utcnow()
    days = max(1, min(int(days or 7), 90))
    start_date = now - timedelta(days=days)
    previous_start = start_date - timedelta(days=days)
    completed = {"completed", "success", "successful"}

    entries = await db["ramp_entries"].find({
        "createdAt": {"$gte": previous_start},
        "status": {"$in": list(completed) + [value.upper() for value in completed]},
    }).sort("createdAt", -1).limit(2000).to_list(2000)
    revenue_rows = await db["settlement_logs"].find({"timestamp": {"$gte": previous_start}, "status": "COMPLETED"}).to_list(2000)
    revenue_by_trade = {str(row.get("trade_id")): row for row in revenue_rows if row.get("trade_id")}

    def kes_value(entry):
        from_asset = str(entry.get("fromAsset") or "").upper()
        to_asset = str(entry.get("toAsset") or "").upper()
        try:
            if from_asset == "KES":
                return abs(float(entry.get("fromAmount") or 0))
            if to_asset == "KES":
                return abs(float(entry.get("toAmount") or 0))
            return abs(float(entry.get("fromAmount") or 0)) * abs(float(entry.get("rate") or 0))
        except (TypeError, ValueError):
            return 0.0

    def entry_revenue(entry):
        row = revenue_by_trade.get(str(entry.get("_id"))) or revenue_by_trade.get(str(entry.get("trade_id")))
        if row:
            try:
                if row.get("profit_kes_equivalent") is not None:
                    return float(row.get("profit_kes_equivalent") or 0)
                return float(row.get("profit_amount") or 0) if str(row.get("profit_currency") or "").upper() == "KES" else 0.0
            except (TypeError, ValueError):
                pass
        for field in ("revenueKes", "revenue", "fee"):
            try:
                value = float(entry.get(field) or 0)
                if value:
                    return value
            except (TypeError, ValueError):
                continue
        return 0.0

    current = [entry for entry in entries if (_normalize_datetime(entry.get("createdAt")) or now) >= start_date]
    previous = [entry for entry in entries if previous_start <= (_normalize_datetime(entry.get("createdAt")) or now) < start_date]
    current_volume = sum(kes_value(entry) for entry in current)
    previous_volume = sum(kes_value(entry) for entry in previous)
    current_revenue = sum(entry_revenue(entry) for entry in current)
    previous_revenue = sum(entry_revenue(entry) for entry in previous)

    async def user_label(user_id):
        label, _, _ = await _get_user_label(db, user_id)
        return label

    sources = []
    actions = []
    for entry in current:
        created_at = _normalize_datetime(entry.get("createdAt")) or now
        source = {
            "transactionId": str(entry.get("_id") or entry.get("id")),
            "timestamp": created_at.isoformat() + "Z",
            "type": str(entry.get("channel") or entry.get("direction") or "Retail"),
            "amount": entry.get("fromAmount") or 0,
            "asset": entry.get("fromAsset") or "KES",
            "kesEquivalent": round(kes_value(entry), 2),
            "revenueKes": round(entry_revenue(entry), 2),
        }
        if created_at.date() == now.date():
            sources.append(source)
        actions.append({
            "id": source["transactionId"],
            "transactionId": source["transactionId"],
            "type": "Retail transaction",
            "details": f"{source['amount']} {source['asset']} via {source['type']} ({entry.get('status', 'completed')})",
            "user": await user_label(entry.get("userId")),
            "timeAgo": _relative_time(created_at),
        })

    pending_trades = await db["ramp_entries"].count_documents({"status": {"$in": ["pending", "processing", "quoted"]}})
    pending_kyc = await db["users"].count_documents({"kycStatus": {"$in": ["pending", "PENDING"]}})
    alerts = await _build_risk_alerts(db)

    def trend(current_value, previous_value):
        return round(((current_value - previous_value) / previous_value) * 100, 1) if previous_value else (100 if current_value else 0)

    return {
        "status": "success",
        "asOf": now.isoformat() + "Z",
        "kpis": {
            "volumeToday": round(current_volume, 2),
            "volumeTrend": trend(current_volume, previous_volume),
            "revenueToday": round(current_revenue, 2),
            "revenueTrend": trend(current_revenue, previous_revenue),
            "pendingTrades": pending_trades,
            "pendingWithdrawalsCount": 0,
            "pendingWithdrawalsValue": 0,
            "pendingKyc": pending_kyc,
            "unmatchedPayments": 0,
            "amlFlags": len(alerts),
        },
        "volumeSources": sources,
        "actions": actions[:20],
        "alerts": alerts,
    }


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


async def _fetch_dealer_rfq(db, rfq_id: str):
    query = {"id": rfq_id}
    rfq = await db["dealer_rfqs"].find_one(query)
    if not rfq and ObjectId:
        try:
            rfq = await db["dealer_rfqs"].find_one({"_id": ObjectId(rfq_id)})
        except Exception:
            rfq = None
    return rfq

# The serializer function to remove MongoDB internal fields and prepare the RFQ for API response
def _serialize_dealer_rfq(rfq: dict) -> dict:
    def serialize_value(value):
        if ObjectId and isinstance(value, ObjectId):
            return str(value)
        if isinstance(value, dict):
            return {key: serialize_value(item) for key, item in value.items() if key != "_id"}
        if isinstance(value, list):
            return [serialize_value(item) for item in value]
        return value

    if not isinstance(rfq, dict):
        return {}
    return serialize_value(rfq)


@router.get("/dealer/rfqs")
async def get_incoming_rfqs(db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    rfqs = await db["dealer_rfqs"].find({}).sort("createdAt", -1).to_list(length=200)
    return {"status": "success", "rfqs": [_serialize_dealer_rfq(item) for item in rfqs]}


@router.get("/dealer/rfqs/{rfq_id}/analysis")
async def get_dealer_rfq_analysis(rfq_id: str, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    rfq = await _fetch_dealer_rfq(db, rfq_id)
    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")

    analysis = await AnalysisEngine(db).analyze(rfq)
    rfq["analysis"] = analysis
    rfq["updatedAt"] = datetime.utcnow()
    await db["dealer_rfqs"].update_one({"id": rfq_id}, {"$set": {"analysis": analysis, "updatedAt": rfq["updatedAt"]}}, upsert=True)
    return {"status": "success", "rfq": _serialize_dealer_rfq(rfq), "analysis": analysis}


@router.post("/dealer/rfqs")
async def create_dealer_rfq(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
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
        "customerId": payload.get("customer_id"),
        "customerName": payload.get("customer_name") or payload.get("customer_id") or "Unknown customer",
        "fromAsset": from_asset,
        "toAsset": to_asset,
        "side": str(payload.get("side", "BUY")).upper(),
        "amount": amount,
        "settlementChannel": payload.get("settlement_channel", "BANK_TO_WALLET"),
        "collectionPhone": payload.get("collection_phone"),
        "destinationWallet": payload.get("destination_wallet"),
        "network": payload.get("network"),
        "channel": "DEALER",
        "status": "quote_ready",
        "createdAt": now,
        "updatedAt": now,
    }
    rfq["analysis"] = await AnalysisEngine(db).analyze(rfq)
    rfq["status"] = "quote_ready" if rfq["analysis"]["passed"] else "blocked"
    await db["dealer_rfqs"].insert_one(rfq)
    response_rfq = _serialize_dealer_rfq(rfq)
    return {"status": "success", "rfq": response_rfq}


@router.post("/dealer/rfqs/{rfq_id}/quote")
async def quote_dealer_rfq(rfq_id: str, payload: dict | None = None, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    rfq = await _fetch_dealer_rfq(db, rfq_id)
    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")

    payload = payload or {}
    send_quote = bool(payload.get("send_quote", True))
    from routes.treasury import get_or_create_rate_book
    rate_book = await get_or_create_rate_book(db)
    platform_default_spread = max(float(rate_book.get("spread_bps", 0) or 0), 0.0)
    # Pricing reference: "live" overlays the real market rate (services/fx_feed.py)
    # onto the rate book for this quote; "rate_book" uses the desk's fixed rates.
    # Live never silently falls back -- if the feed is down the dealer must pick
    # the rate book explicitly.
    price_source = str(payload.get("price_source") or "rate_book").lower()
    market_info: dict = {"priceSource": "rate_book"}
    pair = [str(rfq.get("fromAsset", "")), str(rfq.get("toAsset", ""))]
    if price_source in {"auto", "live", "cbk"}:
        from services.fx_feed import apply_live_rates, apply_cbk_rates, LiveRateUnavailable
        try:
            if price_source == "auto":
                # Default policy: CBK for pairs involving KES, Live market for everything else.
                # If CBK can't price the pair, fall back to Live and say so on the quote.
                if "KES" in {a.upper() for a in pair}:
                    try:
                        rate_book, market_info = await apply_cbk_rates(db, rate_book, pair)
                    except LiveRateUnavailable as cbk_exc:
                        rate_book, market_info = await apply_live_rates(db, rate_book, pair)
                        market_info["autoNote"] = f"CBK unavailable for this pair ({cbk_exc}); priced from Live market"
                else:
                    rate_book, market_info = await apply_live_rates(db, rate_book, pair)
            else:
                apply = apply_cbk_rates if price_source == "cbk" else apply_live_rates
                rate_book, market_info = await apply(db, rate_book, pair)
        except LiveRateUnavailable as exc:
            raise HTTPException(status_code=503, detail=f"{exc}. Switch the pricing source to Rate book to quote anyway.")
    # Dealer-adjustable spread: the frontend (client.ts::quoteDealerRfq) already
    # sends spread_bps, but this endpoint previously ignored it and always used
    # the platform default -- the dealer's entered spread had no effect. Honor
    # it now, clamped to [0, 1000] bps (10%) as a sanity ceiling; fall back to
    # the platform default only when the caller omits it.
    if payload.get("spread_bps") is not None:
        spread_bps = min(max(float(payload.get("spread_bps") or 0), 0.0), 1000.0)
    else:
        spread_bps = platform_default_spread
    from dealer_engine.pricing import build_quote
    quote_result = await build_quote(rate_book, float(rfq.get("amount", 0) or 0), str(rfq.get("fromAsset", "")).upper(), str(rfq.get("toAsset", "")).upper(), str(rfq.get("side", "BUY")).upper(), spread_bps)
    analysis = rfq.get("analysis") or await AnalysisEngine(db).analyze(rfq)
    route = analysis.get("liquidity", {})

    quote = {
        "route": route.get("allocations", []),
        "routeSummary": " + ".join(f"{item['source']} {item['amount']:,.2f}" for item in route.get("allocations", [])) or "No executable liquidity",
        "market_rate": quote_result.get("market_rate"),
        "execution_rate": quote_result.get("execution_rate"),
        "receive_amount": quote_result.get("receive_amount"),
        "fee_amount": quote_result.get("fee_amount"),
        "fee_currency": quote_result.get("fee_currency"),
        "destinationWallet": rfq.get("destinationWallet"),
        "network": rfq.get("network"),
        "spread_bps": spread_bps,
        "expected_pnl": quote_result.get("expected_pnl"),
        "sent": send_quote,
        # Real dealer attribution -- who on the desk actually priced this,
        # surfaced on the merchant's Active Quotes panel instead of a
        # fabricated "your dealer" persona.
        "quotedBy": current_user.get("displayName") or current_user.get("email"),
        **market_info,
    }
    if send_quote:
        quote["sentAt"] = datetime.utcnow()
        # 60s, not the previous 15s -- matches both architecture docs and what
        # a human accept/compliance step actually needs to fit inside.
        quote["expiresAt"] = (datetime.utcnow() + timedelta(seconds=60)).isoformat() + "Z"
    rfq["quote"] = quote
    rfq["status"] = "quoted"
    rfq["updatedAt"] = datetime.utcnow()
    await db["dealer_rfqs"].update_one({"id": rfq_id}, {"$set": {"quote": quote, "status": "quoted", "updatedAt": rfq["updatedAt"]}})

    if send_quote and rfq.get("customerId"):
        try:
            await notify_user(
                db, rfq["customerId"], "settlement", "info",
                "New quote ready",
                f"A firm quote is ready for {rfq_id}: {rfq.get('amount'):,.2f} {rfq.get('fromAsset')} -> "
                f"{float(quote.get('receive_amount') or 0):,.2f} {rfq.get('toAsset')} at {quote.get('execution_rate')}. Expires in 60s.",
                extra={"rfqId": rfq_id, "event": "quote_ready", "route": f"/otc/rfqs/{rfq_id}"},
            )
        except Exception:
            pass  # notification is best-effort, must never block quoting

    return {"status": "success", "rfq": _serialize_dealer_rfq(rfq)}


async def _accept_dealer_rfq_core(db, rfq_id: str, current_user: dict, *, extra_on_success=None) -> dict:
    """
    The actual accept logic (quote validity, ZIGRAM/compliance override,
    revalidation, liquidity reservation, execution record, notifications) --
    factored out so both the admin-initiated accept
    (POST /api/admin/dealer/rfqs/{id}/accept) and the merchant-initiated one
    (POST /api/otc/rfqs/{id}/accept, routes/otc_merchant.py) run the exact
    same path rather than two copies that can drift apart.

    `extra_on_success`, if given, is an async callable `(db, rfq, execution)`
    run right after a successful acceptance. Currently unused by either
    caller -- accepting a quote is purely an agreement (rate + amount), never
    a funds movement; the merchant's obligation to actually pay is only
    created once execute_dealer_rfq issues settlement-specific payment
    instructions, kept as an extension point in case a future caller needs
    to react to acceptance itself.
    """
    rfq = await _fetch_dealer_rfq(db, rfq_id)
    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")

    quote = rfq.get("quote") or {}
    if not quote.get("sent") or not quote.get("expiresAt"):
        raise HTTPException(status_code=409, detail="RFQ does not have a sent quote")
    try:
        expires_at = datetime.fromisoformat(str(quote["expiresAt"]).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=409, detail="Quote expiration is invalid")
    if datetime.utcnow() >= expires_at:
        await db["dealer_rfqs"].update_one({"id": rfq_id}, {"$set": {"status": "expired", "updatedAt": datetime.utcnow()}})
        raise HTTPException(status_code=409, detail="Quote has expired")
    if rfq.get("status") not in {"quoted", "quote_ready"}:
        raise HTTPException(status_code=409, detail=f"RFQ cannot be accepted from {rfq.get('status')} state")

    if rfq.get("complianceOverride"):
        # A compliance officer manually released this specific hold via
        # /dealer/rfqs/{rfq_id}/release -- consume the one-time override
        # instead of calling ZIGRAM again. Does not affect any other RFQ.
        await db["compliance_checks"].insert_one({
            "rfq_id": rfq_id, "outcome": "manual_override_consumed",
            "consumed_by": current_user.get("_id"), "consumed_at": datetime.utcnow(),
        })
        await db["dealer_rfqs"].update_one({"id": rfq_id}, {"$set": {"complianceOverride": False}})
    else:
        from routes.treasury import get_or_create_rate_book
        rate_book = await get_or_create_rate_book(db)
        cleared = await _screen_dealer_rfq(db, rfq=rfq, quote=quote, current_user=current_user, rate_book=rate_book)
        if not cleared:
            await db["dealer_rfqs"].update_one(
                {"id": rfq_id},
                {"$set": {"status": "pending_compliance_review", "updatedAt": datetime.utcnow()}},
            )
            return {
                "status": "pending_compliance_review",
                "rfq": _serialize_dealer_rfq(rfq),
                "message": "This RFQ is held for compliance review. No liquidity has been reserved.",
            }

    analysis = await AnalysisEngine(db).analyze(rfq)
    if not analysis.get("passed"):
        await db["dealer_rfqs"].update_one({"id": rfq_id}, {"$set": {"analysis": analysis, "status": "blocked", "updatedAt": datetime.utcnow()}})
        raise HTTPException(status_code=409, detail="RFQ failed revalidation before acceptance")

    allocations = analysis.get("liquidity", {}).get("allocations", [])
    if not allocations or not analysis.get("liquidity", {}).get("sufficient"):
        raise HTTPException(status_code=409, detail="No executable liquidity is available")
    if any(item.get("settlementMethod") != "internal" for item in allocations):
        raise HTTPException(status_code=409, detail="External liquidity reservation is not configured")
    reservation_id = f"RES-{uuid.uuid4().hex[:8].upper()}"
    if not await TreasuryPositionEngine(db).reserve_route(allocations, reservation_id):
        raise HTTPException(status_code=409, detail="Liquidity could not be reserved")

    execution = {
        "id": f"EXE-{uuid.uuid4().hex[:8].upper()}",
        "rfqId": rfq_id,
        "quote": quote,
        "reservationId": reservation_id,
        "status": "accepted",
        "acceptedBy": current_user.get("_id"),
        "acceptedAt": datetime.utcnow(),
    }
    accepted_at = execution["acceptedAt"]
    await db["dealer_executions"].insert_one(execution)
    rfq["status"] = "accepted"
    rfq["executionId"] = execution["id"]
    rfq["reservationId"] = reservation_id
    rfq["updatedAt"] = accepted_at
    await db["dealer_rfqs"].update_one({"id": rfq_id}, {"$set": {"status": "accepted", "executionId": execution["id"], "reservationId": reservation_id, "updatedAt": accepted_at}})

    if extra_on_success:
        await extra_on_success(db, rfq, execution)

    customer_id = rfq.get("customerId")
    if customer_id:
        try:
            await notify_user(
                db, customer_id, "settlement", "info",
                "Trade accepted",
                f"Your quote for RFQ {rfq_id} has been accepted at rate {quote.get('execution_rate')}. Settlement will follow treasury review.",
                extra={"rfqId": rfq_id, "route": f"/otc/rfqs/{rfq_id}"},
            )
        except Exception:
            pass  # best-effort -- must never block acceptance

    await db["admin_notifications"].insert_one({
        "category": "dealer_rfq",
        "type": "rfq_accepted",
        "title": f"RFQ {rfq_id} accepted",
        "message": f"{current_user.get('email') or current_user.get('_id')} accepted RFQ {rfq_id} for {rfq.get('customerName', 'a customer')}. Awaiting execution.",
        "route": "/admin/institutional-rfqs",
        "isRead": False,
        "sourceRfqId": rfq_id,
        "createdAt": accepted_at,
    })

    # insert_one adds a non-JSON-serializable _id to `execution`; strip it like the rfq.
    return {"status": "success", "rfq": _serialize_dealer_rfq(rfq), "execution": _serialize_dealer_rfq(execution)}


@router.post("/dealer/rfqs/{rfq_id}/accept")
async def accept_dealer_rfq(rfq_id: str, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    return await _accept_dealer_rfq_core(db, rfq_id, current_user)


@router.post("/dealer/rfqs/{rfq_id}/release")
async def release_dealer_rfq_compliance_hold(rfq_id: str, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    """
    Manually clears a ZIGRAM compliance hold on one specific RFQ so it can be
    re-accepted -- a one-time, audited override for this RFQ only. It does
    NOT change ZIGRAM's verdict, does not touch ZIGRAM_CLEAR_STATUSES, and
    does not affect any other RFQ. The next accept attempt on this RFQ skips
    ZIGRAM and consumes the override; a brand new RFQ from the same customer
    is still screened normally.
    """
    ensure_admin(current_user)
    rfq = await _fetch_dealer_rfq(db, rfq_id)
    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")
    if rfq.get("status") != "pending_compliance_review":
        raise HTTPException(status_code=400, detail=f"RFQ is not pending compliance review (status: {rfq.get('status')})")

    now = datetime.utcnow()
    await db["compliance_checks"].insert_one({
        "rfq_id": rfq_id,
        "outcome": "manually_released",
        "released_by": current_user.get("_id"),
        "released_at": now,
    })
    # Always restore to "quote_ready", never "quoted" -- dealer quotes expire
    # in 60 seconds (see quote_dealer_rfq's expiresAt), which any real
    # compliance review will outlast. A fresh quote must be pulled anyway so
    # the client accepts current pricing, not a stale one from before the hold.
    await db["dealer_rfqs"].update_one(
        {"id": rfq_id},
        {"$set": {"status": "quote_ready", "complianceOverride": True, "updatedAt": now}},
    )
    return {
        "status": "success",
        "message": "Compliance hold released. Pull a fresh quote for this RFQ, then accept -- the compliance override will be honored on that next accept.",
    }


def _build_settlement_payment_instructions(rfq: dict, settlement_id: str) -> dict | None:
    """
    Where a merchant self-service settlement's `fromAsset` leg should be
    sent, generated only now (execution time) rather than at accept -- the
    merchant should never be told to pay before their quote is locked in.
    Memo is unique per settlement (never reused), following the same
    JASIRI-style memo-tagging pattern routes/stellar.py's deposit flow
    already uses, so treasury can attribute an incoming payment to this
    exact settlement when they run confirm_customer_funds.

    Crypto legs point at the same treasury deposit addresses routes/
    stellar.py::get_deposit_info already exposes for retail deposits --
    reusing the identical env vars rather than a second address config.
    Fiat legs (the merchant sending KES/XAF/etc in) have no automated
    collection account yet -- surfaced as a manual instruction for the
    dealer to arrange over chat, matching the "manual confirmation
    discipline" already used everywhere else in this file.
    """
    if rfq.get("origin") != "merchant_self_service":
        return None
    asset = str(rfq.get("fromAsset", "")).upper()
    amount = float(rfq.get("amount", 0) or 0)
    memo = f"SETTLE-{settlement_id}"

    crypto_networks = {
        "USDA": ("cardano", os.environ.get("MASTER_WALLET_ADDRESS", "")),
        "ADA": ("cardano", os.environ.get("MASTER_WALLET_ADDRESS", "")),
        "USDC": ("celo", os.environ.get("CELO_HOT_WALLET_ADDRESS", "")),
        "USDT": ("tron", os.environ.get("TRON_MASTER_ADDRESS", "")),
        "CUSD": ("celo", os.environ.get("CELO_HOT_WALLET_ADDRESS", "")),
        "XLM": ("stellar", os.environ.get("STELLAR_MASTER_ADDRESS", "")),
    }
    if asset in crypto_networks:
        network, address = crypto_networks[asset]
        return {
            "kind": "crypto",
            "asset": asset,
            "amount": amount,
            "network": network,
            "address": address,
            "memo": memo if network == "stellar" else None,
        }

    return {
        "kind": "fiat",
        "asset": asset,
        "amount": amount,
        "reference": memo,
        "note": "Your dealer will share bank/mobile money account details for this reference in the settlement chat.",
    }


@router.post("/dealer/rfqs/{rfq_id}/execute")
async def execute_dealer_rfq(rfq_id: str, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    rfq = await _fetch_dealer_rfq(db, rfq_id)
    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")

    if rfq.get("status") != "accepted":
        raise HTTPException(status_code=409, detail="RFQ must be accepted before execution")

    now = datetime.utcnow()
    settlement = {
        "id": f"SET-{uuid.uuid4().hex[:8].upper()}",
        "rfqId": rfq_id,
        # Starts at "pending" -- TreasurySettlements.tsx's actionsFor() only
        # offers "Review"/"Fail settlement" from here. Nothing auto-advances
        # this anymore; every further transition goes through
        # POST /dealer/settlements/{id}/action, driven by a human treasury
        # action (or, at submit_transfer, a real Daraja/Cardano call).
        "status": "pending",
        "customer": {
            "id": rfq.get("customerId"),
            "name": rfq.get("customerName"),
        },
        # Immutable snapshot of what was actually agreed -- market rate,
        # execution rate and spread at accept time -- so treasury (and the
        # merchant, on their own settlement view) can see the margin behind
        # this specific trade without cross-referencing the RFQ separately,
        # and so a later rate-book change can never retroactively change
        # what this settlement says was agreed.
        "fromAsset": rfq.get("fromAsset"),
        "toAsset": rfq.get("toAsset"),
        "amount": rfq.get("amount"),
        "quote": rfq.get("quote"),
        "destinationWallet": rfq.get("destinationWallet"),
        "bankDetails": rfq.get("bankDetails"),
        "settlementChannel": rfq.get("settlementChannel"),
        "collectionPhone": rfq.get("collectionPhone"),
        "network": rfq.get("network"),
        "reservationId": rfq.get("reservationId"),
        "legs": {
            "fiat": {
                "status": "pending",
                "amount": (rfq.get("quote") or {}).get("receive_amount", rfq.get("amount", 0)),
                "asset": rfq.get("toAsset", "KES"),
            },
            "crypto": {
                "status": "pending",
                "amount": rfq.get("amount", 0),
                "asset": rfq.get("fromAsset", "digital asset"),
            },
        },
        "blockchain": {"network": rfq.get("network"), "status": "not submitted", "txHash": None},
        "providerStatus": None,
        "paymentEvidence": None,
        "audit": [{
            "action": "executed",
            "fromStatus": "accepted",
            "toStatus": "pending",
            "performedBy": current_user.get("_id"),
            "performedAt": now,
        }],
        "createdAt": now,
        "updatedAt": now,
    }
    settlement["paymentInstructions"] = _build_settlement_payment_instructions(rfq, settlement["id"])
    rfq["status"] = "executed"
    rfq["settlementId"] = settlement["id"]
    rfq["paymentInstructions"] = settlement["paymentInstructions"]
    rfq["updatedAt"] = now
    await db["dealer_rfqs"].update_one(
        {"id": rfq_id},
        {"$set": {"status": "executed", "settlementId": settlement["id"], "paymentInstructions": settlement["paymentInstructions"], "updatedAt": now}},
    )
    await db["dealer_settlements"].update_one({"id": settlement["id"]}, {"$set": settlement}, upsert=True)

    await db["admin_notifications"].insert_one({
        "category": "dealer_settlement",
        "type": "settlement_awaiting_review",
        "title": f"Settlement {settlement['id']} awaiting treasury review",
        "message": f"RFQ {rfq_id} for {rfq.get('customerName', 'a customer')} was executed and needs treasury review.",
        "route": "/admin/settlements",
        "isRead": False,
        "sourceSettlementId": settlement["id"],
        "createdAt": now,
    })

    return {"status": "success", "settlement": settlement, "rfq": _serialize_dealer_rfq(rfq)}


@router.get("/dealer/settlements")
async def get_dealer_settlements(db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    settlements = await db["dealer_settlements"].find({}).sort("createdAt", -1).to_list(length=200)
    return {"status": "success", "demoMode": {"allowSelfSubmit": settings.otc_demo_allow_self_submit}, "settlements": [
        {key: value for key, value in s.items() if key != "_id"} for s in settlements
    ]}


@router.get("/dealer/settlements/{settlement_id}")
async def get_dealer_settlement(settlement_id: str, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    settlement = await db["dealer_settlements"].find_one({"id": settlement_id})
    if not settlement and ObjectId:
        try:
            settlement = await db["dealer_settlements"].find_one({"_id": ObjectId(settlement_id)})
        except Exception:
            settlement = None
    if not settlement:
        raise HTTPException(status_code=404, detail="Settlement not found")

    return {"status": "success", "settlement": {key: value for key, value in settlement.items() if key != "_id"}}


# Settlement action state machine -- see TreasurySettlements.tsx::actionsFor
# for the exact per-status action set the frontend already offers; this must
# match it exactly or a button in the UI will 404/400.
async def _send_dealer_momo_payout(phone: str, amount: float, external_id: str, momo_provider: str = "Airtel") -> dict:
    """
    Fiat-out leg of a dealer settlement, via the same Airtel/Mamlaka gateway
    routes/ramp.py's retail off-ramp already uses (AIRTEL_API_BASE_URL/
    AIRTEL_API_USERNAME/AIRTEL_API_PASSWORD). Deliberately does not touch
    retail_wallets or 2FA -- those are retail-specific; a dealer settlement's
    "debit" is the treasury reservation already handled by
    TreasuryPositionEngine.spend_route/release_route in the caller.

    Returns {"success": True, "provider": ..., "reference": ...} or
    {"success": False, "error": ...}.
    """
    gateway_url = os.environ.get("AIRTEL_API_BASE_URL", "").rstrip("/")
    api_username = os.environ.get("AIRTEL_API_USERNAME", "")
    api_password = os.environ.get("AIRTEL_API_PASSWORD", "")
    if not gateway_url or not api_username or not api_password:
        return {"success": False, "error": "Airtel/Mamlaka gateway credentials are not configured."}

    phone_local = str(phone).strip().replace(" ", "").replace("-", "")
    if phone_local.startswith("+"):
        phone_local = phone_local[1:]
    if phone_local.startswith("254"):
        phone_local = phone_local[3:]
    if phone_local.startswith("0"):
        phone_local = phone_local[1:]

    requested_provider = str(momo_provider or "Airtel").strip().lower()
    is_mpesa, valid_prefix = resolve_momo_provider_and_validate(phone_local, requested_provider)
    if not valid_prefix:
        return {"success": False, "error": f"Unsupported MSISDN for {'M-Pesa' if is_mpesa else 'Airtel'} payout: {phone}"}

    payload = {
        "impalaMerchantId": api_username,
        "recipientPhone": f"254{phone_local}",
        "amount": int(amount),
        "currency": "KES",
        "mobileMoneySP": "M-Pesa" if is_mpesa else "Airtel",
        "externalId": external_id,
    }
    try:
        async with httpx.AsyncClient() as client:
            auth_response = await client.get(f"{gateway_url}/", auth=(api_username, api_password), timeout=15.0)
            auth_response.raise_for_status()
            auth_body = auth_response.json()
            access_token = auth_body.get("token") if isinstance(auth_body, dict) else None
            if not access_token:
                return {"success": False, "error": "Gateway authentication response did not include a token."}
            headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
            response = await client.post(f"{gateway_url}/mobile/transfer", json=payload, headers=headers, timeout=15.0)
            if response.is_error:
                return {"success": False, "error": response.text}
            return {
                "success": True,
                "provider": "M-Pesa" if is_mpesa else "Airtel",
                "reference": external_id,
            }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


_SETTLEMENT_TRANSITIONS: dict[str, dict[str, str]] = {
    "pending": {"review": "treasury_review", "fail_settlement": "failed"},
    "treasury_review": {
        "confirm_customer_funds": "funds_confirmed",
        "release_reservation": "reservation_released",
        "fail_settlement": "failed",
    },
    "funds_confirmed": {"approve_crypto_transfer": "transfer_approved", "fail_settlement": "failed"},
    "transfer_approved": {"submit_transfer": "transfer_pending", "fail_settlement": "failed"},
    "transfer_pending": {
        "confirm_fiat_receipt": "fiat_confirmed",
        "confirm_crypto_receipt": "crypto_confirmed",
        "fail_settlement": "failed",
    },
    "fiat_confirmed": {"mark_reconciled": "reconciled"},
    "crypto_confirmed": {"mark_reconciled": "reconciled"},
}

# Terminal statuses that release/spend the treasury reservation and should
# notify the customer + admin.
_RELEASE_ON = {"failed", "reservation_released"}
_SPEND_ON = {"reconciled"}

# Merchant-visible copy per settlement status -- previously only "reconciled"
# and the release states pushed a live notification, so the merchant's
# dashboard had no way to show real settlement progress (funds received ->
# confirming -> completed) short of polling and hoping the status changed.
# Every transition in _SETTLEMENT_TRANSITIONS now has an entry here so the
# in-progress tracker on the merchant dashboard can update live off the same
# /ws/dashboard notification channel onboarding/acceptance already use.
_SETTLEMENT_PROGRESS_COPY: dict[str, tuple[str, str, str]] = {
    "treasury_review": ("info", "Settlement under review", "Treasury is reviewing your settlement."),
    "funds_confirmed": ("info", "Funds received", "Your incoming funds have been confirmed by treasury -- settlement is now processing."),
    "transfer_approved": ("info", "Settlement approved", "Your settlement has been approved and is being submitted for transfer."),
    "transfer_pending": ("info", "Transfer submitted", "Your outbound transfer has been submitted and is confirming."),
    "fiat_confirmed": ("info", "Transfer confirmed", "Your settlement's transfer has been confirmed -- finalizing now."),
    "crypto_confirmed": ("info", "Transfer confirmed", "Your settlement's transfer has been confirmed -- finalizing now."),
    "reconciled": ("success", "Settlement completed", "Your OTC settlement has been completed and reconciled."),
    "failed": ("warning", "Settlement did not complete", "Your OTC settlement was marked 'failed'. Contact support for details."),
    "reservation_released": ("warning", "Settlement cancelled", "Your OTC settlement's reservation was released. Contact support for details."),
}


@router.post("/dealer/settlements/{settlement_id}/action")
async def act_on_dealer_settlement(settlement_id: str, payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    """
    Drives a dealer settlement through the treasury review/approval/transfer
    workflow TreasurySettlements.tsx is already built against. This is the
    endpoint client.ts::actOnDealerSettlement calls -- it did not exist
    before, so every button on that page 404'd.

    "Escrow" discipline lives here: the crypto/fiat outbound leg is never
    submitted (submit_transfer) until treasury has explicitly confirmed the
    customer's incoming leg via confirm_customer_funds, matching how a real
    OTC desk holds its leg until the counterparty's funds are confirmed in.
    """
    ensure_admin(current_user)
    action = str(payload.get("action") or "").strip()
    if not action:
        raise HTTPException(status_code=400, detail="action is required")

    settlement = await db["dealer_settlements"].find_one({"id": settlement_id})
    if not settlement:
        raise HTTPException(status_code=404, detail="Settlement not found")

    current_status = str(settlement.get("status") or "pending").lower()
    allowed = _SETTLEMENT_TRANSITIONS.get(current_status, {})
    if action not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Action '{action}' is not allowed from status '{current_status}'",
        )
    next_status = allowed[action]

    now = datetime.utcnow()
    updates: dict = {"status": next_status, "updatedAt": now}
    performed_by = current_user.get("email") or current_user.get("_id")
    demo_self_submit = False

    rfq = await _fetch_dealer_rfq(db, settlement.get("rfqId")) or {}

    if (
        action == "fail_settlement"
        and str(settlement.get("settlementChannel") or "").upper() == "WALLET_BALANCE"
        and ((settlement.get("legs") or {}).get("fiat") or {}).get("status") in {"submitted", "confirmed"}
    ):
        raise HTTPException(status_code=400, detail="Proceeds were already credited to the merchant's wallet; this settlement can no longer be failed.")

    if action == "confirm_customer_funds":
        # Merchant self-service: treasury must state the amount that actually arrived and it
        # must match what the merchant owes (the settlement's inbound leg). A mismatch is
        # refused so a short or wrong payment can't be waved through.
        expected_in = float(((settlement.get("legs") or {}).get("crypto") or {}).get("amount", 0) or 0)
        expected_asset = str(((settlement.get("legs") or {}).get("crypto") or {}).get("asset") or "")
        if rfq.get("origin") == "merchant_self_service" and expected_in > 0:
            raw_amount = payload.get("payment_amount")
            if raw_amount in (None, ""):
                raise HTTPException(status_code=400, detail=f"Enter the Payment amount that actually arrived (expected {expected_in:,.2f} {expected_asset}).")
            try:
                received = float(raw_amount)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Payment amount must be a number.")
            if abs(received - expected_in) > 0.01:
                raise HTTPException(
                    status_code=400,
                    detail=f"Amount mismatch: {received:,.2f} entered but {expected_in:,.2f} {expected_asset} is expected. Do not confirm a short or excess payment; contact the merchant or fail the settlement.",
                )
        updates["paymentEvidence"] = {
            "status": "customer_funds_confirmed",
            "provider": payload.get("payment_provider"),
            "reference": payload.get("payment_reference"),
            "amount": payload.get("payment_amount"),
            "expectedAmount": expected_in or None,
            "confirmedBy": performed_by,
            "confirmedAt": now,
        }
        # Merchant self-service quote-then-fund flow: this is the moment the
        # merchant's inbound leg (sent against the paymentInstructions
        # execute_dealer_rfq issued) is confirmed to have actually landed --
        # log it as a Collection so it shows on the merchant's Collections/
        # Transactions History pages. No institutional_wallets.available
        # touched: this money was sent to fund this one settlement, not
        # deposited as a standing balance.
        if rfq.get("origin") == "merchant_self_service" and rfq.get("customerId"):
            from institutional_wallet_utils import log_settlement_ledger_entry
            fiat_leg = (settlement.get("legs") or {}).get("fiat") or {}
            crypto_leg = (settlement.get("legs") or {}).get("crypto") or {}
            channel = str(settlement.get("settlementChannel") or "").upper()
            # The merchant's inbound leg is whichever leg they're the SOURCE
            # of -- WALLET_TO_BANK/BANK_TRANSFER means they send crypto in;
            # BANK_TO_WALLET/WALLET_TRANSFER means they send fiat in.
            inbound_leg = crypto_leg if (rfq.get("origin") == "merchant_self_service" or channel in {"WALLET_TO_BANK", "BANK_TRANSFER"}) else fiat_leg
            if float(inbound_leg.get("amount", 0) or 0) > 0:
                await log_settlement_ledger_entry(
                    db, str(rfq["customerId"]), direction="in",
                    asset=inbound_leg.get("asset", ""), amount=float(inbound_leg.get("amount", 0) or 0),
                    source="settlement_funding", related_rfq_id=rfq.get("id"), related_settlement_id=settlement_id,
                )

    elif action == "submit_transfer":
        # This is where real value actually moves -- only reachable after
        # confirm_customer_funds -> approve_crypto_transfer, i.e. only once
        # treasury has explicitly confirmed the customer's leg landed.
        #
        # settlementChannel decides which leg WE deliver (the other leg is
        # the customer's inbound payment, already covered by
        # confirm_customer_funds above) -- see SETTLEMENT_CHANNELS in
        # DealerWorkspaceWizard.tsx: "BANK_TO_WALLET"/"WALLET_TRANSFER" means
        # the customer receives crypto into a wallet (we deliver crypto);
        # "WALLET_TO_BANK"/"BANK_TRANSFER" means the customer receives fiat
        # into a bank/momo account (we deliver fiat).
        #
        # Legs land at "submitted" here, not "confirmed" -- confirm_fiat_
        # receipt/confirm_crypto_receipt (below) is the separate step that
        # verifies the customer actually received it and marks it confirmed.
        #
        # Maker-checker: this is the action that actually fires a real
        # Cardano/Celo/mobile-money transfer, so the admin who approved the
        # transfer (approve_crypto_transfer, above) may not be the same one
        # who submits it -- dual control on the one step that moves real
        # money, cheap to enforce since both are already separate, audited
        # transitions in _SETTLEMENT_TRANSITIONS.
        approver = next(
            (str(entry.get("performedBy")) for entry in reversed(settlement.get("audit") or [])
             if entry.get("action") == "approve_crypto_transfer"),
            None,
        )
        if approver and approver == str(performed_by):
            if settings.otc_demo_allow_self_submit:
                demo_self_submit = True  # DEMO ONLY: stamped on the audit entry below
            else:
                raise HTTPException(
                    status_code=403,
                    detail="Maker-checker: the admin who approved this transfer cannot also submit it. Have another admin submit it.",
                )

        channel = str(settlement.get("settlementChannel") or "").upper()
        deliver_crypto = channel in {"BANK_TO_WALLET", "WALLET_TRANSFER"}
        legs = settlement.get("legs") or {}

        if channel == "WALLET_BALANCE":
            # Merchant chose to receive the converted funds into their Jasiri
            # wallet: value "moves" by crediting institutional_wallets (the
            # balance they can then pay beneficiaries from), no external rail.
            out_leg = legs.get("fiat") or {}
            out_amount = float(out_leg.get("amount", 0) or 0)
            out_asset = str(out_leg.get("asset") or "").upper()
            if out_amount <= 0 or not out_asset or not rfq.get("customerId"):
                raise HTTPException(status_code=400, detail="Settlement has no receive amount to credit")
            from institutional_wallet_utils import credit_available
            await credit_available(
                db, str(rfq["customerId"]), out_asset, out_amount,
                source="conversion_proceeds", related_rfq_id=rfq.get("id"), related_settlement_id=settlement_id,
            )
            updates["legs.fiat.status"] = "submitted"
            updates["paymentEvidence"] = {**(settlement.get("paymentEvidence") or {}), "status": "credited_to_wallet", "reference": f"WALLET-{settlement_id}"}

        elif channel == "TO_BANK":
            # Treasury settles fiat to the merchant's bank account only (never
            # mobile money). Sent manually from treasury's bank; the bank
            # transfer reference is required so the payment is traceable.
            out_leg = legs.get("fiat") or {}
            out_amount = float(out_leg.get("amount", 0) or 0)
            bank = settlement.get("bankDetails") or {}
            reference = str(payload.get("payment_reference") or "").strip()
            if out_amount <= 0 or not bank.get("accountNumber"):
                raise HTTPException(status_code=400, detail="Settlement has no bank details or amount to pay out")
            if not reference:
                raise HTTPException(status_code=400, detail="Enter the bank transfer reference (Payment reference) before submitting")
            updates["providerStatus"] = "submitted"
            updates["paymentEvidence"] = {
                **(settlement.get("paymentEvidence") or {}),
                "status": "submitted", "provider": payload.get("payment_provider") or f"Bank transfer to {bank.get('bankName')}",
                "reference": reference,
            }
            updates["legs.fiat.status"] = "submitted"

        elif deliver_crypto or channel == "TO_EXTERNAL_WALLET":
            ext = channel == "TO_EXTERNAL_WALLET"
            ext_network = str(settlement.get("network") or "").lower()
            destination = settlement.get("destinationWallet")
            # Merchant self-service legs: "fiat" is always the OUTBOUND leg
            # (toAsset / receive amount), "crypto" the inbound one.
            crypto_leg = (legs.get("fiat") if ext else legs.get("crypto")) or {}
            asset = str(crypto_leg.get("asset") or "").upper()
            amount = float(crypto_leg.get("amount", 0) or 0)
            if not destination:
                raise HTTPException(status_code=400, detail="Settlement has no destinationWallet to send crypto to")
            if amount <= 0:
                raise HTTPException(status_code=400, detail="Settlement has no crypto amount to send")

            if asset == "USDA" and (not ext or ext_network == "cardano"):
                from cardano.wallet import CardanoWallet
                from cardano.usda import send_usda
                import asyncio as _asyncio
                try:
                    treasury_wallet = CardanoWallet(0)
                    tx_hash = await _asyncio.to_thread(send_usda, treasury_wallet, destination, amount)
                except Exception as exc:
                    # Do not advance status on failure -- stays at
                    # transfer_approved so treasury can retry.
                    raise HTTPException(status_code=502, detail=f"Cardano USDA transfer failed: {exc}")
                updates["blockchain"] = {"network": "cardano", "status": "submitted", "txHash": tx_hash}

            elif asset in {"USDC", "USDT", "CUSD"} and (not ext or ext_network == "celo"):
                from routes.swap_engine import settle_crypto_on_celo
                celo_asset = "cUSD" if asset == "CUSD" else asset
                result = await settle_crypto_on_celo(destination, celo_asset, amount)
                if not result.get("success"):
                    raise HTTPException(status_code=502, detail=f"Celo {celo_asset} transfer failed: {result.get('error')}")
                updates["blockchain"] = {"network": "celo", "status": "submitted", "txHash": result.get("tx_hash")}

            elif ext:
                # Network the platform doesn't send on automatically (Stellar,
                # Polygon, BEP20, Tron, Ethereum): treasury sends from their own
                # wallet and records the transaction hash here.
                manual_hash = str(payload.get("tx_hash") or "").strip()
                if not manual_hash:
                    raise HTTPException(status_code=400, detail=f"{ext_network or 'This network'} is settled manually: send {amount:,.2f} {asset} to the wallet, then enter the Blockchain TX hash")
                updates["blockchain"] = {"network": ext_network, "status": "submitted", "txHash": manual_hash, "manual": True}

            else:
                raise HTTPException(status_code=400, detail=f"Unsupported crypto settlement asset: {asset}")

            updates["legs.fiat.status" if ext else "legs.crypto.status"] = "submitted"

        else:
            phone = settlement.get("collectionPhone")
            fiat_leg = legs.get("fiat") or {}
            amount = float(fiat_leg.get("amount", 0) or 0)
            if not phone:
                raise HTTPException(status_code=400, detail="Settlement has no collectionPhone to send fiat to")
            if amount <= 0:
                raise HTTPException(status_code=400, detail="Settlement has no fiat amount to send")

            momo_provider = rfq.get("network") or "Airtel"
            momo_result = await _send_dealer_momo_payout(phone, amount, settlement_id, momo_provider)
            if not momo_result.get("success"):
                raise HTTPException(status_code=502, detail=f"Mobile money payout failed: {momo_result.get('error')}")

            updates["providerStatus"] = "submitted"
            updates["paymentEvidence"] = {
                **(settlement.get("paymentEvidence") or {}),
                "status": "submitted",
                "provider": momo_result.get("provider"),
                "reference": momo_result.get("reference"),
            }
            updates["legs.fiat.status"] = "submitted"

    elif action in {"confirm_fiat_receipt", "confirm_crypto_receipt"}:
        prior_evidence = settlement.get("paymentEvidence") or {}
        updates["providerStatus"] = payload.get("payment_provider") or "confirmed"
        updates["paymentEvidence"] = {
            **prior_evidence,
            "status": "received",
            # Keep the reference/provider recorded at submit_transfer when the
            # confirmation form is left blank.
            "provider": payload.get("payment_provider") or prior_evidence.get("provider"),
            "reference": payload.get("payment_reference") or prior_evidence.get("reference"),
            "amount": payload.get("payment_amount") if payload.get("payment_amount") is not None else prior_evidence.get("amount"),
            "receivedAt": now,
        }
        leg_key = "fiat" if action == "confirm_fiat_receipt" else "crypto"
        if rfq.get("origin") == "merchant_self_service":
            leg_key = "fiat"  # merchant's receiving (outbound) leg is always legs.fiat
        updates[f"legs.{leg_key}.status"] = "confirmed"

    reservation_id = settlement.get("reservationId")
    if reservation_id:
        engine = TreasuryPositionEngine(db)
        if next_status in _RELEASE_ON:
            await engine.release_route(reservation_id)
        elif next_status in _SPEND_ON:
            await engine.spend_route(reservation_id)

    # Log the delivered (outbound) leg as a Payout on the merchant's ledger
    # once the settlement is fully reconciled -- this is quote-then-fund, not
    # the old pre-funded standing-balance model, so there is no
    # institutional_wallets.locked figure to release/spend here; nothing was
    # ever locked at accept time (see routes/otc_merchant.py::
    # merchant_accept_rfq). A failed/released settlement needs no unwind for
    # the same reason -- the merchant's funds were never held by the
    # platform in the first place.
    if rfq.get("origin") == "merchant_self_service" and next_status in _SPEND_ON and rfq.get("customerId"):
        from institutional_wallet_utils import log_settlement_ledger_entry
        fiat_leg = (settlement.get("legs") or {}).get("fiat") or {}
        crypto_leg = (settlement.get("legs") or {}).get("crypto") or {}
        channel = str(settlement.get("settlementChannel") or "").upper()
        # Whichever leg WE delivered to the external destination is the
        # merchant's outbound Payout -- the mirror of the inbound leg logged
        # as a Collection in confirm_customer_funds above.
        outbound_leg = fiat_leg if (channel in {"WALLET_TO_BANK", "BANK_TRANSFER", "TO_BANK", "TO_EXTERNAL_WALLET"}) else crypto_leg
        # WALLET_BALANCE proceeds were already credited to the wallet at
        # submit_transfer (logged as an "in" entry) -- no external payout leg.
        if channel != "WALLET_BALANCE" and float(outbound_leg.get("amount", 0) or 0) > 0:
            await log_settlement_ledger_entry(
                db, str(rfq["customerId"]), direction="out",
                asset=outbound_leg.get("asset", ""), amount=float(outbound_leg.get("amount", 0) or 0),
                source="settlement_payout", related_rfq_id=rfq.get("id"), related_settlement_id=settlement_id,
            )

    audit_entry = {
        "action": action,
        "fromStatus": current_status,
        "toStatus": next_status,
        "performedBy": performed_by,
        "performedAt": now,
        "note": payload.get("note") or ("DEMO MODE: maker-checker bypassed (same admin approved and submitted)" if demo_self_submit else None),
    }

    await db["dealer_settlements"].update_one(
        {"id": settlement_id},
        {"$set": updates, "$push": {"audit": audit_entry}},
    )
    if rfq.get("id") and next_status in {"reconciled", "failed", "reservation_released"}:
        await db["dealer_rfqs"].update_one({"id": rfq["id"]}, {"$set": {"status": next_status, "updatedAt": now}})

    customer_id = rfq.get("customerId")
    progress_copy = _SETTLEMENT_PROGRESS_COPY.get(next_status)
    if customer_id and progress_copy:
        severity, title, message = progress_copy
        try:
            await notify_user(
                db, customer_id, "settlement", severity, title,
                f"{message} (Settlement {settlement_id})",
                extra={"settlementId": settlement_id, "rfqId": rfq.get("id"), "settlementStatus": next_status, "route": "/otc/settlements"},
            )
        except Exception:
            pass  # notification is best-effort, must never block a settlement action

    await db["admin_notifications"].insert_one({
        "category": "dealer_settlement",
        "type": f"settlement_{next_status}",
        "title": f"Settlement {settlement_id} -> {next_status.replace('_', ' ')}",
        "message": f"{performed_by} moved settlement {settlement_id} from {current_status} to {next_status} via '{action}'.",
        "route": "/admin/settlements",
        "isRead": False,
        "sourceSettlementId": settlement_id,
        "createdAt": now,
    })

    updated = await db["dealer_settlements"].find_one({"id": settlement_id})
    return {"status": "success", "settlement": {key: value for key, value in updated.items() if key != "_id"}}


@router.get("/dealer/clients")
async def get_dealer_clients(db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    users = await db["users"].find({}, {"displayName": 1, "name": 1, "email": 1, "phone": 1, "walletAddress": 1}).limit(500).to_list(length=500)
    clients = [{
        "id": str(user.get("_id")),
        "name": user.get("displayName") or user.get("name") or user.get("email") or "Unknown customer",
        "phone": user.get("phone"),
        "walletAddress": user.get("walletAddress"),
    } for user in users]
    return {"status": "success", "clients": clients}

@router.get("/analytics/chart-data")
async def get_chart_analytics(days: int = 7, scope: str = "retail", db=Depends(get_db)):
    days = max(1, min(int(days or 7), 90))
    now = datetime.utcnow()
    start_date = now - timedelta(days=days)
    entries = await db["ramp_entries"].find({
        "createdAt": {"$gte": start_date},
        "status": {"$in": ["completed", "COMPLETED", "Completed", "success", "successful"]},
    }).sort("createdAt", 1).to_list(2000)
    revenue_rows = await db["settlement_logs"].find({"timestamp": {"$gte": start_date}, "status": "COMPLETED"}).to_list(2000)
    revenue_by_trade = {str(row.get("trade_id")): row for row in revenue_rows if row.get("trade_id")}
    chart_by_date = {}
    for entry in entries:
        created_at = _normalize_datetime(entry.get("createdAt")) or now
        from_asset = str(entry.get("fromAsset") or "").upper()
        to_asset = str(entry.get("toAsset") or "").upper()
        try:
            volume = abs(float(entry.get("fromAmount") or 0)) if from_asset == "KES" else abs(float(entry.get("toAmount") or 0)) if to_asset == "KES" else abs(float(entry.get("fromAmount") or 0)) * abs(float(entry.get("rate") or 0))
        except (TypeError, ValueError):
            volume = 0.0
        key = created_at.strftime("%b %d")
        chart_by_date.setdefault(key, {"date": key, "volume": 0.0, "revenue": 0.0})
        chart_by_date[key]["volume"] += volume
        revenue_row = revenue_by_trade.get(str(entry.get("_id")))
        if revenue_row:
            revenue = float(revenue_row.get("profit_kes_equivalent") or 0)
            if not revenue_row.get("profit_kes_equivalent") and str(revenue_row.get("profit_currency") or "").upper() == "KES":
                revenue = float(revenue_row.get("profit_amount") or 0)
        else:
            revenue = float(entry.get("revenueKes") or entry.get("revenue") or 0)
        chart_by_date[key]["revenue"] += revenue
    return {"status": "success", "chartData": [{**row, "volume": round(row["volume"], 2), "revenue": round(row["revenue"], 2)} for row in chart_by_date.values()]}

@router.get("/retail-transactions")
async def get_all_retail_transactions(userId: str = None, limit: int = 200, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    if not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Admin role required.")
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

        provider_report = e.get("providerReport") if isinstance(e.get("providerReport"), dict) else {}
        tx_hash, network, explorer_url = _resolve_tx_explorer(e)
        formatted_entries.append({
            "id": str(e["_id"]),
            "createdAt": e.get("createdAt", datetime.utcnow()).isoformat() + "Z" if e.get("createdAt") else None,
            "customerName": customer_name,
            "direction": e.get("direction", "swap"),
            "fromAmount": e.get("fromAmount", 0), "fromAsset": e.get("fromAsset", ""),
            "toAmount": e.get("toAmount", 0), "toAsset": e.get("toAsset", ""),
            "status": e.get("status", "pending"),
            "providerReference": e.get("providerReference"),
            "secureId": e.get("secureId") or (e.get("providerReport") or {}).get("secureId"),
            "externalId": e.get("externalId") or (e.get("providerReport") or {}).get("externalId") or e.get("providerReference"),
            "providerReport": provider_report,
            "providerCallbackReference": provider_report.get("reference") or provider_report.get("transactionReference"),
            "mobileMoneyProvider": e.get("mobileMoneyProvider"),
            "providerStatus": e.get("providerStatus"),
            "failureReason": e.get("error_reason") or (e.get("providerReport") or {}).get("message") or (e.get("providerReport") or {}).get("transactionReport") or ((e.get("providerReport") or {}).get("transaction") or {}).get("message"),
            "txHash": tx_hash,
            "network": network,
            "explorerUrl": explorer_url,
            "counterparty": e.get("counterparty"),
        })

    return {"status": "success", "entries": formatted_entries}

# 🟢 FIX: Use the payload Pydantic model and handle tx_id formats safely
@router.patch("/retail-transactions/{tx_id}/status")
async def moderate_transaction(tx_id: str, payload: TxStatusUpdate, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    if not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Admin role required.")

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

    if entry.get("status") == "completed" and new_status == "completed":
        return {"status": "success", "message": "Transaction already completed; wallet not credited again."}

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
                await _apply_wallet_delta_once(
                    db, user_id, wallet_asset, amount, str(entry.get("_id")), "appliedRampCredits"
                )

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
            # So the notification can deep-link straight to the RFQ/settlement
            # it's about, instead of just the generic queue page -- an admin
            # clicking "New message on RFQ-X" had no way to land on RFQ-X
            # specifically.
            "sourceRfqId": item.get("sourceRfqId"),
            "sourceSettlementId": item.get("sourceSettlementId"),
        })

    return {"status": "success", "unreadCount": unread_count, "notifications": formatted}


async def _build_zigram_holds(db):
    """
    Surfaces everything ZIGRAM has held for manual compliance review -- retail
    Mobile Money deposits/withdrawals, internal swaps, and dealer/OTC RFQs --
    plus recent screening errors, so an admin can see and act on them without
    querying MongoDB directly. See routes/ramp.py::_screen_ramp_transaction /
    _screen_swap_transaction and this file's _screen_dealer_rfq for what
    writes into `compliance_checks`.
    """
    holds = []

    ramp_holds = await db["ramp_entries"].find(
        {"status": "pending_compliance_review"}
    ).sort("createdAt", -1).limit(100).to_list(100)
    for entry in ramp_holds:
        holds.append({
            "id": str(entry.get("_id")),
            "type": "ramp" if entry.get("direction") in {"on", "off"} else "swap",
            "direction": entry.get("direction"),
            "fromAsset": entry.get("fromAsset"),
            "toAsset": entry.get("toAsset"),
            "amount": entry.get("fromAmount"),
            "userId": str(entry.get("userId")),
            "createdAt": entry.get("createdAt"),
            "releaseEndpoint": f"/api/ramp/admin/release/{entry.get('_id')}",
        })

    rfq_holds = await db["dealer_rfqs"].find(
        {"status": "pending_compliance_review"}
    ).sort("updatedAt", -1).limit(100).to_list(100)
    for rfq in rfq_holds:
        holds.append({
            "id": rfq.get("id"),
            "type": "dealer_rfq",
            "customer": rfq.get("customerName") or rfq.get("customerId"),
            "fromAsset": rfq.get("fromAsset"),
            "toAsset": rfq.get("toAsset"),
            "amount": rfq.get("amount"),
            "createdAt": rfq.get("updatedAt"),
            "releaseEndpoint": f"/api/admin/dealer/rfqs/{rfq.get('id')}/release",
        })

    # Recent screening errors (ZIGRAM unreachable/misconfigured) even where the
    # underlying transaction row may have already been marked held above --
    # useful for spotting outages vs. genuine flags.
    recent_errors = await db["compliance_checks"].find(
        {"outcome": "error", "created_at": {"$gte": datetime.utcnow() - timedelta(hours=24)}}
    ).sort("created_at", -1).limit(50).to_list(50)

    return holds, len(recent_errors)


@router.get("/compliance/monitoring")
async def get_compliance_monitoring(db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)

    aml_flags = await _build_aml_flags(db)
    risk_alerts = await _build_risk_alerts(db)
    zigram_holds, zigram_errors_24h = await _build_zigram_holds(db)

    return {
        "status": "success",
        "kpis": {
            "amlFlags": len(aml_flags),
            "pepMatches": 0,
            "sanctions": 0,
            "riskAlerts": len(risk_alerts),
            "highRiskAlerts": len([alert for alert in risk_alerts if alert.get("severity") == "high"]),
            "zigramHolds": len(zigram_holds),
            "zigramErrors24h": zigram_errors_24h,
        },
        "amlFlags": aml_flags,
        "riskAlerts": risk_alerts,
        "zigramHolds": zigram_holds,
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
    await notify_user(
        db, safe_obj_id(id), "kyc", "success",
        "KYC approved",
        "Your identity verification was approved. You can now trade and withdraw.",
    )
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
    await notify_user(
        db, safe_obj_id(id), "kyc", "error",
        "KYC rejected",
        "Your identity verification was rejected. Please review your submission and try again.",
    )
    return {"status": "rejected"}


# --- Institutional (OTC merchant) onboarding review -------------------------
# Separate from the retail KYC queue above -- different collection
# (institutional_profiles, not users.kycStatus), different depth (business
# overview, directors, shareholders, PEPs, documents). See
# routes/otc_merchant.py for where merchants submit these.

@router.get("/compliance/institutional-onboarding")
async def get_institutional_onboarding_queue(db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    profiles = await db["institutional_profiles"].find(
        {"onboardingStatus": "under_review"}
    ).sort("submittedAt", -1).limit(100).to_list(100)
    for p in profiles:
        p.pop("_id", None)
    return {"status": "success", "kpis": {"pendingOnboarding": len(profiles)}, "queue": profiles}


@router.get("/compliance/institutional-onboarding/{user_id}")
async def get_institutional_onboarding_detail(user_id: str, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    profile = await db["institutional_profiles"].find_one({"userId": user_id})
    if not profile:
        raise HTTPException(status_code=404, detail="Institutional profile not found")
    profile.pop("_id", None)
    return {"status": "success", "profile": profile}


@router.post("/compliance/institutional-onboarding/{user_id}/approve")
async def approve_institutional_onboarding(user_id: str, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    now = datetime.utcnow()
    res = await db["institutional_profiles"].update_one(
        {"userId": user_id},
        {"$set": {
            "onboardingStatus": "approved",
            "reviewedAt": now,
            "reviewedBy": current_user.get("_id"),
        }},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Institutional profile not found")
    await notify_user(
        db, user_id, "onboarding", "success",
        "Onboarding approved",
        "Your institutional account is approved. You can now fund your account and request OTC settlements.",
        extra={"route": "/otc/overview"},
    )
    return {"status": "approved"}


@router.post("/compliance/institutional-onboarding/{user_id}/reject")
async def reject_institutional_onboarding(user_id: str, payload: dict | None = None, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    reason = (payload or {}).get("reason", "Does not meet onboarding requirements")
    now = datetime.utcnow()
    res = await db["institutional_profiles"].update_one(
        {"userId": user_id},
        {"$set": {
            "onboardingStatus": "rejected",
            "reviewedAt": now,
            "reviewedBy": current_user.get("_id"),
            "reviewNotes": reason,
        }},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Institutional profile not found")
    await notify_user(
        db, user_id, "onboarding", "error",
        "Onboarding rejected",
        f"Your institutional onboarding was rejected: {reason}",
        extra={"route": "/otc/onboarding"},
    )
    return {"status": "rejected"}


# --- OTC desk analytics -------------------------------------------------
# The dealer/treasury-side equivalent of the merchant dashboard: rolls up
# dealer_rfqs/dealer_settlements/institutional_profiles into the KPIs a
# dealer actually needs at a glance (active merchants, funnel, settlement
# throughput, who's driving volume) instead of the raw per-RFQ queue
# DealerWorkspaceLive already shows. Deliberately separate from
# /operations-overview above, which is retail-only (ramp_entries) and
# doesn't touch the dealer_rfqs/dealer_settlements collections at all.

@router.get("/otc/analytics-overview")
async def get_otc_analytics_overview(days: int = 30, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    days = max(1, min(int(days or 30), 180))
    now = datetime.utcnow()
    start_date = now - timedelta(days=days)

    profiles = await db["institutional_profiles"].find(
        {}, {"onboardingStatus": 1, "legalName": 1, "businessName": 1, "userId": 1},
    ).to_list(length=2000)
    approved_merchants = [p for p in profiles if p.get("onboardingStatus") == "approved"]

    rfqs = await db["dealer_rfqs"].find({"createdAt": {"$gte": start_date}}).to_list(length=5000)
    settlements = await db["dealer_settlements"].find({"createdAt": {"$gte": start_date}}).to_list(length=5000)

    funnel: dict[str, int] = {}
    for rfq in rfqs:
        status = rfq.get("status", "unknown")
        funnel[status] = funnel.get(status, 0) + 1

    reconciled = [s for s in settlements if s.get("status") == "reconciled"]
    settlement_minutes: list[float] = []
    for settlement in reconciled:
        created = _normalize_datetime(settlement.get("createdAt"))
        reconciled_at = None
        for entry in settlement.get("audit") or []:
            if entry.get("toStatus") == "reconciled":
                reconciled_at = _normalize_datetime(entry.get("performedAt"))
        if created and reconciled_at:
            settlement_minutes.append((reconciled_at - created).total_seconds() / 60)
    avg_settlement_minutes = round(sum(settlement_minutes) / len(settlement_minutes), 1) if settlement_minutes else None

    volume_by_merchant: dict[str, float] = {}
    counted_statuses = {"accepted", "executed", "reconciled"}
    for rfq in rfqs:
        if rfq.get("status") in counted_statuses:
            name = rfq.get("customerName") or rfq.get("customerId") or "Unknown"
            volume_by_merchant[name] = volume_by_merchant.get(name, 0) + float(rfq.get("amount", 0) or 0)
    top_merchants = sorted(volume_by_merchant.items(), key=lambda kv: kv[1], reverse=True)[:8]

    active_settlement_statuses = {"reconciled", "failed", "reservation_released"}

    return {
        "status": "success",
        "asOf": now.isoformat(),
        "kpis": {
            "activeMerchants": len(approved_merchants),
            "totalMerchants": len(profiles),
            "totalRfqs": len(rfqs),
            "acceptedVolumeCount": sum(1 for rfq in rfqs if rfq.get("status") in counted_statuses),
            "pendingComplianceReview": funnel.get("pending_compliance_review", 0),
            "activeSettlements": sum(1 for s in settlements if s.get("status") not in active_settlement_statuses),
            "reconciledSettlements": len(reconciled),
            "avgSettlementMinutes": avg_settlement_minutes,
        },
        "rfqFunnel": [{"status": status, "count": count} for status, count in sorted(funnel.items(), key=lambda kv: -kv[1])],
        "topMerchants": [{"name": name, "volume": volume} for name, volume in top_merchants],
    }


@router.post("/institutional-wallets/{user_id}/credit")
async def credit_institutional_wallet(user_id: str, payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    """
    Treasury manually confirms an incoming merchant deposit (bank transfer or
    on-chain) and credits it to that merchant's available balance -- same
    manual-confirmation discipline as confirm_customer_funds uses for dealer
    settlements. Real deposit automation (a watcher matching provider
    callbacks/on-chain deposits to this) is a later phase.
    """
    ensure_admin(current_user)
    from institutional_wallet_utils import credit_available
    asset = str(payload.get("asset", "")).strip().upper()
    amount = float(payload.get("amount", 0) or 0)
    reference = payload.get("reference", "")
    if not asset or amount <= 0:
        raise HTTPException(status_code=400, detail="asset and a positive amount are required")

    await credit_available(db, user_id, asset, amount)
    await db["institutional_wallet_deposits"].insert_one({
        "userId": user_id, "asset": asset, "amount": amount,
        "reference": reference, "confirmedBy": current_user.get("_id"),
        "confirmedAt": datetime.utcnow(),
    })
    await notify_user(
        db, user_id, "wallet", "success",
        "Deposit confirmed",
        f"{amount:,.2f} {asset} has been credited to your available balance.",
        extra={"route": "/otc/wallet"},
    )
    return {"status": "success"}


@router.get("/finance/customers")
async def get_customers_list(db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    users = await db["users"].find().sort("createdAt", -1).limit(100).to_list(100)
    customers = []
    for u in users:
        wallet = await db["retail_wallets"].find_one({"userId": {"$in": build_user_id_candidates(u["_id"])}})
        total_vol = sum(float(v) for k, v in (wallet or {}).items() if k not in ["_id", "userId"] and isinstance(v, (int, float)))
        
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
            # The customer modal needs the complete, server-authoritative KYC
            # record. This route is admin-only; retail APIs never expose it.
            "kycDetails": {
                "fullName": kyc_details.get("fullName"),
                "idNumber": kyc_details.get("idNumber"),
                "phone": kyc_details.get("phone"),
                "email": kyc_details.get("email"),
                "documentName": kyc_details.get("documentName"),
                "documentMimeType": kyc_details.get("documentMimeType"),
                "documentSize": kyc_details.get("documentSize"),
                "documentDataUrl": kyc_details.get("documentDataUrl"),
            },
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


# --- Beneficiary payout requests (treasury review) -------------------------
# A merchant's self-service request to pay a saved beneficiary out of their
# settled balance (routes/otc_merchant.py::create_payout_request). Goes
# through the same "a human confirms before money moves" discipline as
# every other payout path here -- approve/reject is a compliance decision,
# mark_paid is treasury recording that the transfer was actually sent
# (manually today; wiring settle_bank_transfer/settle_mobilemoney_via_
# flutterwave to fire automatically here is a real next step, deliberately
# not done in the same change that introduced the request itself).

@router.get("/otc/payout-requests")
async def list_otc_payout_requests(status: str | None = None, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    query = {"status": status} if status else {}
    rows = await db["institutional_payout_requests"].find(query).sort("createdAt", -1).to_list(length=500)
    for r in rows:
        r.pop("_id", None)
    return {"status": "success", "requests": rows}


@router.post("/otc/payout-requests/{request_id}/action")
async def act_on_otc_payout_request(request_id: str, payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    action = str((payload or {}).get("action") or "").strip()
    if action not in {"approve", "reject", "mark_paid"}:
        raise HTTPException(status_code=400, detail="action must be approve, reject, or mark_paid")

    request_doc = await db["institutional_payout_requests"].find_one({"id": request_id})
    if not request_doc:
        raise HTTPException(status_code=404, detail="Payout request not found")

    current_status = request_doc.get("status")
    allowed_from = {"approve": "pending_review", "reject": "pending_review", "mark_paid": "approved"}
    if current_status != allowed_from[action]:
        raise HTTPException(status_code=400, detail=f"Cannot {action} a request in status '{current_status}'")

    now = datetime.utcnow()
    next_status = {"approve": "approved", "reject": "rejected", "mark_paid": "paid"}[action]
    reference = str((payload or {}).get("reference") or "").strip() or None
    notes = str((payload or {}).get("notes") or "").strip() or None
    updates = {
        "status": next_status, "updatedAt": now,
        "reviewedBy": current_user.get("displayName") or current_user.get("email"),
        "reviewedAt": now,
    }
    if notes:
        updates["reviewNotes"] = notes
    if reference:
        updates["reference"] = reference

    if action == "mark_paid":
        # Debit first (atomic, guarded) so a paid payout can never leave the
        # merchant's balance untouched, and a short balance blocks the status change.
        asset_key = str(request_doc.get("currency", "")).upper()
        pay_amount = float(request_doc.get("amount", 0) or 0)
        debited = await db["institutional_wallets"].update_one(
            {"_id": request_doc["merchantId"], f"{asset_key}.available": {"$gte": pay_amount}},
            {"$inc": {f"{asset_key}.available": -pay_amount}, "$set": {"updatedAt": now}},
        )
        if not debited.modified_count:
            raise HTTPException(status_code=400, detail="Merchant's available balance no longer covers this payout")

    await db["institutional_payout_requests"].update_one({"id": request_id}, {"$set": updates})

    if action == "mark_paid":
        from institutional_wallet_utils import log_settlement_ledger_entry
        await log_settlement_ledger_entry(
            db, request_doc["merchantId"], direction="out",
            asset=request_doc.get("currency", ""), amount=float(request_doc.get("amount", 0) or 0),
            source="beneficiary_payout",
        )

    notify_copy = {
        "approve": ("success", "Payout request approved", f"Your payout of {request_doc.get('amount'):,.2f} {request_doc.get('currency')} to {request_doc.get('beneficiaryName')} was approved and is being processed."),
        "reject": ("warning", "Payout request rejected", f"Your payout request to {request_doc.get('beneficiaryName')} was rejected. {notes or ''}".strip()),
        "mark_paid": ("success", "Payout sent", f"{request_doc.get('amount'):,.2f} {request_doc.get('currency')} was sent to {request_doc.get('beneficiaryName')}." + (f" Ref: {reference}" if reference else "")),
    }[action]
    try:
        await notify_user(
            db, request_doc["merchantId"], "payout_request", notify_copy[0], notify_copy[1], notify_copy[2],
            extra={"route": "/otc/payouts", "payoutRequestId": request_id},
        )
    except Exception:
        pass

    return {"status": "success"}


# --- Merchant funding + collection-method requests (treasury desk) ----------
# Treasury's side of "Fund Balance" and "Request Collection Method" on the
# merchant portal. Funding is only ever credited here, after a human has
# confirmed the money actually arrived -- same manual-confirmation discipline
# as dealer settlements.

def _strip_id(rows: list) -> list:
    for r in rows:
        r.pop("_id", None)
    return rows


@router.get("/otc/funding-requests")
async def list_otc_funding_requests(status: str | None = None, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    query = {"status": status} if status else {}
    rows = await db["institutional_funding_requests"].find(query).sort("createdAt", -1).to_list(length=300)
    return {"status": "success", "requests": _strip_id(rows)}


@router.post("/otc/funding-requests/{request_id}/action")
async def act_on_otc_funding_request(request_id: str, payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    """credit: treasury confirmed the money arrived -> credits the merchant's
    wallet (amount may differ from what was requested). reject: closes it."""
    ensure_admin(current_user)
    action = str((payload or {}).get("action") or "").strip()
    if action not in {"credit", "reject"}:
        raise HTTPException(status_code=400, detail="action must be credit or reject")

    req = await db["institutional_funding_requests"].find_one({"id": request_id})
    if not req:
        raise HTTPException(status_code=404, detail="Funding request not found")
    if req.get("status") != "pending":
        raise HTTPException(status_code=400, detail=f"Request is already '{req.get('status')}'")

    now = datetime.utcnow()
    reviewer = current_user.get("displayName") or current_user.get("email")
    reference = str((payload or {}).get("reference") or "").strip() or None
    notes = str((payload or {}).get("notes") or "").strip() or None
    updates: dict = {"updatedAt": now, "reviewedBy": reviewer, "reviewedAt": now}
    if reference:
        updates["reference"] = reference
    if notes:
        updates["reviewNotes"] = notes

    if action == "credit":
        credited = float((payload or {}).get("amount") or req.get("amount") or 0)
        if credited <= 0:
            raise HTTPException(status_code=400, detail="Credit amount must be positive")
        from institutional_wallet_utils import credit_available
        await credit_available(db, req["merchantId"], req["currency"], credited, source="treasury_funding")
        updates.update({"status": "credited", "creditedAmount": credited})
        copy = ("success", "Wallet funded", f"{credited:,.2f} {req['currency']} has been credited to your wallet." + (f" Ref: {reference}" if reference else ""))
    else:
        updates["status"] = "rejected"
        copy = ("warning", "Funding request rejected", f"Your {req['currency']} funding request was not approved. {notes or ''}".strip())

    await db["institutional_funding_requests"].update_one({"id": request_id}, {"$set": updates})
    try:
        await notify_user(db, req["merchantId"], "funding_request", copy[0], copy[1], copy[2], extra={"route": "/otc/wallet", "fundingRequestId": request_id})
    except Exception:
        pass
    return {"status": "success"}


@router.get("/otc/collection-requests")
async def list_otc_collection_requests(status: str | None = None, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    query = {"status": status} if status else {}
    rows = await db["institutional_collection_requests"].find(query).sort("createdAt", -1).to_list(length=300)
    return {"status": "success", "requests": _strip_id(rows)}


@router.post("/otc/collection-requests/{request_id}/action")
async def act_on_otc_collection_request(request_id: str, payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    """provision: the collection method is live; `details` (account number,
    paybill, instructions) is shown to the merchant. reject: closes it."""
    ensure_admin(current_user)
    action = str((payload or {}).get("action") or "").strip()
    if action not in {"provision", "reject"}:
        raise HTTPException(status_code=400, detail="action must be provision or reject")

    req = await db["institutional_collection_requests"].find_one({"id": request_id})
    if not req:
        raise HTTPException(status_code=404, detail="Collection request not found")
    if req.get("status") != "pending":
        raise HTTPException(status_code=400, detail=f"Request is already '{req.get('status')}'")

    details = str((payload or {}).get("details") or "").strip()
    if action == "provision" and not details:
        raise HTTPException(status_code=400, detail="Provide the details the merchant should use (account, paybill, instructions)")

    now = datetime.utcnow()
    updates = {
        "status": "provisioned" if action == "provision" else "rejected",
        "updatedAt": now, "reviewedAt": now,
        "reviewedBy": current_user.get("displayName") or current_user.get("email"),
    }
    if details:
        updates["details"] = details
    await db["institutional_collection_requests"].update_one({"id": request_id}, {"$set": updates})

    label = str(req.get("method", "")).replace("_", " ")
    copy = ("success", "Collection method live", f"Your {label} for {req.get('currency')} is ready. Open Collections to see how to use it.") if action == "provision" \
        else ("warning", "Collection request rejected", f"Your {label} request for {req.get('currency')} was not approved.")
    try:
        await notify_user(db, req["merchantId"], "collection_request", copy[0], copy[1], copy[2], extra={"route": "/otc/collections", "collectionRequestId": request_id})
    except Exception:
        pass
    return {"status": "success"}


# --- Treasury reconciliation -----------------------------------------------
# treasury_positions.total is NET tradeable inventory: conversion proceeds
# credited to a merchant come off it at reconcile, while top-ups and payouts
# move cash and merchant liabilities together (net zero). So real cash held
# should equal:   net inventory  +  everything merchants hold in their wallets.
# This view compares that expectation with the real bank / M-Pesa / on-chain
# balance treasury records, and flags any variance.

_RECON_TOLERANCE = 0.01


@router.get("/otc/reconciliation")
async def get_otc_reconciliation(db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)

    positions = {p["asset"].upper(): p for p in await db["treasury_positions"].find({}).to_list(length=200) if p.get("asset")}

    liabilities: dict[str, dict] = {}
    merchant_counts: dict[str, int] = {}
    async for w in db["institutional_wallets"].find({}):
        for asset, bal in w.items():
            if asset in {"_id", "updatedAt"} or not isinstance(bal, dict):
                continue
            key = asset.upper()
            row = liabilities.setdefault(key, {"available": 0.0, "locked": 0.0})
            row["available"] += float(bal.get("available", 0) or 0)
            row["locked"] += float(bal.get("locked", 0) or 0)
            if (bal.get("available") or 0) or (bal.get("locked") or 0):
                merchant_counts[key] = merchant_counts.get(key, 0) + 1

    pending_payouts: dict[str, float] = {}
    async for r in db["institutional_payout_requests"].find({"status": {"$in": ["pending_review", "approved"]}}):
        k = str(r.get("currency", "")).upper()
        pending_payouts[k] = pending_payouts.get(k, 0.0) + float(r.get("amount", 0) or 0)

    unconfirmed_funding: dict[str, float] = {}
    async for r in db["institutional_funding_requests"].find({"status": "pending"}):
        k = str(r.get("currency", "")).upper()
        unconfirmed_funding[k] = unconfirmed_funding.get(k, 0.0) + float(r.get("amount", 0) or 0)

    snapshots: dict[str, dict] = {}
    async for s in db["treasury_balance_snapshots"].find({}).sort("recordedAt", 1):
        snapshots[str(s["asset"]).upper()] = s

    assets = sorted(set(positions) | set(liabilities) | set(snapshots))
    rows = []
    for asset in assets:
        pos = positions.get(asset) or {}
        net_total = float(pos.get("total", pos.get("available", 0)) or 0)
        reserved = float(pos.get("reserved", 0) or 0) + float(pos.get("pending", 0) or 0)
        held = liabilities.get(asset, {"available": 0.0, "locked": 0.0})
        merchant_held = held["available"] + held["locked"]
        expected = net_total + merchant_held
        snap = snapshots.get(asset)
        actual = float(snap["balance"]) if snap else None
        variance = round(actual - expected, 4) if actual is not None else None
        if actual is None:
            status = "no_snapshot"
        elif abs(variance) <= _RECON_TOLERANCE:
            status = "balanced"
        else:
            status = "over" if variance > 0 else "short"
        rows.append({
            "asset": asset,
            "tracked": bool(pos),
            "netInventory": round(net_total, 4),
            "reserved": round(reserved, 4),
            "availableToTrade": round(max(net_total - reserved, 0), 4),
            "merchantHeld": round(merchant_held, 4),
            "merchantsHolding": merchant_counts.get(asset, 0),
            "pendingPayouts": round(pending_payouts.get(asset, 0.0), 4),
            "unconfirmedFunding": round(unconfirmed_funding.get(asset, 0.0), 4),
            "expectedHoldings": round(expected, 4),
            "actualBalance": actual,
            "actualSource": (snap or {}).get("source"),
            "actualAsOf": (snap or {}).get("recordedAt"),
            "variance": variance,
            "status": status,
        })
    return {"status": "success", "rows": rows}


@router.post("/otc/reconciliation/snapshot")
async def record_balance_snapshot(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    """Treasury records the REAL balance it sees (bank statement, M-Pesa
    float, on-chain wallet) for one asset. Append-only, so history is kept."""
    ensure_admin(current_user)
    asset = str((payload or {}).get("asset") or "").strip().upper()
    try:
        balance = float((payload or {}).get("balance"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="balance must be a number")
    if not asset or balance < 0:
        raise HTTPException(status_code=400, detail="asset and a non-negative balance are required")
    await db["treasury_balance_snapshots"].insert_one({
        "asset": asset, "balance": balance,
        "source": str((payload or {}).get("source") or "manual").strip() or "manual",
        "note": (payload or {}).get("note"),
        "recordedBy": current_user.get("displayName") or current_user.get("email"),
        "recordedAt": datetime.utcnow(),
    })
    return {"status": "success"}


@router.get("/otc/market-rates")
async def get_otc_market_rates(assets: str = "KES,UGX,NGN", refresh: bool = False, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    """Live USDT market rate vs the desk's rate book, so a dealer can see how
    far the book has drifted from the market before pricing a quote."""
    ensure_admin(current_user)
    from routes.treasury import get_or_create_rate_book
    from services.fx_feed import get_live_snapshot, market_rate, LiveRateUnavailable
    book = await get_or_create_rate_book(db)
    book_rates = book.get("usd_base_rates", {})
    try:
        snap = await get_live_snapshot(db, force=refresh)
    except LiveRateUnavailable as exc:
        return {"status": "unavailable", "detail": str(exc), "rows": []}
    rows = []
    for asset in [a.strip().upper() for a in assets.split(",") if a.strip()]:
        live = market_rate(snap, "USDT", asset)
        bk = (book_rates.get(asset) / book_rates.get("USDT", 1.0)) if book_rates.get(asset) else None
        rows.append({
            "asset": asset,
            "liveRate": round(live, 6) if live else None,
            "rateBookRate": round(bk, 6) if bk else None,
            "deviationBps": round((bk - live) / live * 10000, 1) if live and bk else None,
            "source": (snap.get("sources") or {}).get(asset),
        })
    from services.fx_feed import get_cbk_snapshot
    live_kes = market_rate(snap, "USD", "KES")
    cbk_snap = None
    try:
        cbk_snap = await get_cbk_snapshot(force=refresh)
    except LiveRateUnavailable:
        pass
    cbk_info = None
    if cbk_snap:
        cbk_info = {
            "usdKes": cbk_snap["usdKes"], "source": cbk_snap["provider"], "cbkDate": cbk_snap.get("cbkDate"),
            "fetchedAt": cbk_snap["fetchedAt"], "automatic": True,
            "deviationVsLiveBps": round((cbk_snap["usdKes"] - live_kes) / live_kes * 10000, 1) if live_kes else None,
        }
        for row in rows:
            v = market_rate(cbk_snap, "USDT", row["asset"])
            row["cbkRate"] = round(v, 6) if v else None
    else:
        cbk = await db["fx_reference_rates"].find_one({"_id": "CBK_USD_KES"}) or {}
        if cbk.get("rate"):
            cbk_info = {
                "usdKes": cbk["rate"], "source": f"manual entry by {cbk.get('enteredBy')}", "enteredAt": cbk.get("enteredAt"),
                "automatic": False,
                "deviationVsLiveBps": round((cbk["rate"] - live_kes) / live_kes * 10000, 1) if live_kes else None,
            }
    return {
        "status": "success", "rows": rows, "cbk": cbk_info,
        "provider": snap["provider"], "cadence": snap["cadence"],
        "providerUpdatedAt": snap["providerUpdatedAt"], "fetchedAt": snap["fetchedAt"],
        "ageSeconds": snap.get("ageSeconds", 0), "usdtUsd": snap.get("usdtUsd"), "degraded": snap.get("degraded"),
        "providerErrors": snap.get("providerErrors") or [],
    }


@router.post("/otc/market-rates/cbk-reference")
async def set_cbk_reference(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    """CBK publishes no usable API (its web table stops at Jan 2024), so treasury
    enters the day's CBK USD/KES mean from the CBK site; dealers see it beside
    the live market as a cross-check. Never used to price a quote by itself."""
    ensure_admin(current_user)
    try:
        rate = float((payload or {}).get("rate"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="rate must be a number")
    if not 50 < rate < 500:
        raise HTTPException(status_code=400, detail="That does not look like a USD/KES rate")
    await db["fx_reference_rates"].update_one(
        {"_id": "CBK_USD_KES"},
        {"$set": {"rate": rate, "enteredBy": current_user.get("displayName") or current_user.get("email"), "enteredAt": datetime.utcnow()}},
        upsert=True,
    )
    return {"status": "success"}


# --- Settlement proof uploads ----------------------------------------------
# Treasury attaches evidence (bank slip, explorer screenshot, exchange withdrawal
# PDF) to a settlement. Stored like avatars/KYC documents: base64 data URI in its
# own collection, with only light metadata on the settlement itself so the queue
# stays fast.

_EVIDENCE_MIME = {"image/png", "image/jpeg", "image/webp", "application/pdf"}
_EVIDENCE_MAX_BYTES = 3_000_000


@router.post("/dealer/settlements/{settlement_id}/evidence")
async def upload_settlement_evidence(settlement_id: str, payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    settlement = await db["dealer_settlements"].find_one({"id": settlement_id}, {"status": 1})
    if not settlement:
        raise HTTPException(status_code=404, detail="Settlement not found")

    data_url = str((payload or {}).get("dataUrl") or "")
    if not data_url.startswith("data:") or "," not in data_url:
        raise HTTPException(status_code=400, detail="Expected a data URI (data:<type>;base64,...)")
    header, b64 = data_url.split(",", 1)
    mime = header.split(";")[0].removeprefix("data:")
    if mime not in _EVIDENCE_MIME:
        raise HTTPException(status_code=400, detail="Proof must be a PNG, JPEG, WEBP image or a PDF")
    if (len(b64) * 3) // 4 > _EVIDENCE_MAX_BYTES:
        raise HTTPException(status_code=400, detail="File is too large (max 3MB)")

    now = datetime.utcnow()
    evidence_id = f"EV-{uuid.uuid4().hex[:8].upper()}"
    meta = {
        "id": evidence_id,
        "filename": str((payload or {}).get("filename") or "proof")[:120],
        "mime": mime,
        "size": (len(b64) * 3) // 4,
        "note": ((payload or {}).get("note") or None),
        "step": settlement.get("status"),
        "uploadedBy": current_user.get("displayName") or current_user.get("email"),
        "uploadedAt": now,
    }
    await db["settlement_evidence"].insert_one({**meta, "settlementId": settlement_id, "dataUrl": data_url})
    await db["dealer_settlements"].update_one(
        {"id": settlement_id},
        {"$push": {"evidence": meta}, "$set": {"updatedAt": now}},
    )
    return {"status": "success", "evidence": meta}


@router.get("/dealer/settlements/{settlement_id}/evidence/{evidence_id}")
async def get_settlement_evidence(settlement_id: str, evidence_id: str, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    ensure_admin(current_user)
    doc = await db["settlement_evidence"].find_one({"id": evidence_id, "settlementId": settlement_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return {"status": "success", "filename": doc.get("filename"), "mime": doc.get("mime"), "dataUrl": doc.get("dataUrl")}
