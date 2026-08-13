from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from datetime import datetime
from typing import Any
import uuid
import json
import hashlib
import hmac
import requests
import asyncio
import os
import httpx
from urllib.parse import parse_qs

from database import get_db
from services.safaricom_daraja import DarajaService
from routes.auth import get_current_user, get_current_user_with_role, is_admin_role
from routes.treasury import get_or_create_rate_book, compute_swap_quote_from_book

router = APIRouter(prefix="/api/ramp", tags=["Ramp & Swaps"])
callback_router = APIRouter(prefix="/api/v1/callbacks", tags=["Airtel Callbacks"])
mam_laka = DarajaService()

# Safe MongoDB ObjectId converter
try:
    from bson import ObjectId
except ImportError:
    ObjectId = None

def safe_object_id(val):
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

class RampExecute(BaseModel):
    direction: str
    channel: str
    from_asset: str
    to_asset: str
    amount: float
    rate: float
    fee: float
    counterparty: str


class ReconcileDepositsRequest(BaseModel):
    references: list[str]
    result_status: str = "completed"
    confirm_paid: bool = False
    provider_report: dict[str, Any] | None = None


class ReconcileWithdrawalsRequest(BaseModel):
    references: list[str]
    result_status: str = "completed"
    confirm_paid: bool = False
    provider_report: dict[str, Any] | None = None


class CorrectCompletedWithdrawalsRequest(BaseModel):
    references: list[str]
    refund_wallet: bool = True
    provider_report: dict[str, Any]


def _extract_reference_from_payload(payload: dict, tx: dict) -> str | None:
    """Extract reference id from varying Airtel/gateway payload shapes."""
    candidates = [
        tx.get("reference"),
        tx.get("id"),
        tx.get("externalId"),
        tx.get("clientReference"),
        tx.get("client_reference"),
        tx.get("merchantReference"),
        tx.get("transactionReference"),
        tx.get("gatewayReference"),
        tx.get("gateway_reference"),
        payload.get("reference"),
        payload.get("id"),
        payload.get("externalId"),
        payload.get("clientReference"),
        payload.get("client_reference"),
        payload.get("merchantReference"),
        payload.get("transactionReference"),
        payload.get("gatewayReference"),
        payload.get("gateway_reference"),
    ]

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    candidates.extend([
        data.get("reference"),
        data.get("id"),
        data.get("externalId"),
        data.get("merchantReference"),
    ])

    for val in candidates:
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


async def _extract_callback_payload(request: Request) -> tuple[dict, str]:
    """Parse callback payload defensively to handle JSON, form-urlencoded, or query-only callbacks."""
    raw_bytes = await request.body()
    raw_text = raw_bytes.decode("utf-8", errors="ignore") if raw_bytes else ""

    payload: dict[str, Any] = {}

    try:
        parsed = await request.json()
        if isinstance(parsed, dict):
            payload = parsed
        elif parsed is not None:
            payload = {"data": parsed}
    except Exception:
        if raw_text and "=" in raw_text:
            form_data = parse_qs(raw_text, keep_blank_values=True)
            payload = {k: (v[0] if len(v) == 1 else v) for k, v in form_data.items()}
            nested = payload.get("payload")
            if isinstance(nested, str):
                try:
                    nested_obj = json.loads(nested)
                    if isinstance(nested_obj, dict):
                        payload = nested_obj
                except Exception:
                    pass

    if not payload and request.query_params:
        payload = dict(request.query_params)

    return payload, raw_text


def _verify_airtel_callback_signature(request: Request, raw_text: str) -> dict[str, Any]:
    """Verify an HMAC callback signature only when direct-callback verification is enabled."""
    enabled = os.environ.get("AIRTEL_CALLBACK_AUTH_ENABLED", "false").strip().lower() == "true"
    header_name = os.environ.get("AIRTEL_CALLBACK_SIGNATURE_HEADER", "X-Airtel-Signature").strip()
    signature = request.headers.get(header_name, "").strip() if header_name else ""

    auth = {
        "enabled": enabled,
        "header": header_name,
        "signaturePresent": bool(signature),
        "valid": None,
    }
    if not enabled:
        return auth

    secret = os.environ.get("AIRTEL_CALLBACK_HASH_KEY", "")
    if not secret or not signature:
        auth["valid"] = False
        auth["reason"] = "missing shared key or signature header"
        return auth

    expected = hmac.new(
        secret.encode("utf-8"),
        raw_text.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    provided = signature.removeprefix("sha256=").strip().lower()
    auth["valid"] = hmac.compare_digest(provided, expected)
    if not auth["valid"]:
        auth["reason"] = "signature mismatch"
    return auth


async def _handle_airtel_callback(request: Request, db, source: str):
    payload, raw_text = await _extract_callback_payload(request)
    callback_auth = _verify_airtel_callback_signature(request, raw_text)

    event_id = f"CB_{uuid.uuid4().hex[:10].upper()}"
    headers = {
        "content-type": request.headers.get("content-type", ""),
        "user-agent": request.headers.get("user-agent", ""),
        "x-forwarded-for": request.headers.get("x-forwarded-for", ""),
        "x-real-ip": request.headers.get("x-real-ip", ""),
    }
    callback_event = {
        "_id": event_id,
        "source": source,
        "path": str(request.url.path),
        "headers": headers,
        "payload": payload,
        "rawBody": raw_text[:4000],
        "authentication": callback_auth,
        "receivedAt": datetime.utcnow(),
        "processed": False,
    }
    await db["airtel_callback_events"].insert_one(callback_event)

    if callback_auth["enabled"] and callback_auth["valid"] is not True:
        await db["airtel_callback_events"].update_one(
            {"_id": event_id},
            {"$set": {"error": "Callback authentication failed", "processedAt": datetime.utcnow()}},
        )
        raise HTTPException(status_code=401, detail="Invalid Airtel callback signature")

    try:
        # Compute a short payload reference for logging to avoid f-string brace issues
        _ref = _extract_reference_from_payload(payload, payload.get('transaction') or {})
        print(f"🔔 Incoming callback path={request.url.path} source={source} payload_ref={_ref}")
        result = await _process_airtel_c2b_payload(payload, db)
        print(f"🔔 Callback processor result: {result}")
        await db["airtel_callback_events"].update_one(
            {"_id": event_id},
            {
                "$set": {
                    "processed": True,
                    "result": result,
                    "processedAt": datetime.utcnow(),
                }
            },
        )
        return result
    except Exception as exc:
        await db["airtel_callback_events"].update_one(
            {"_id": event_id},
            {
                "$set": {
                    "processed": False,
                    "error": str(exc),
                    "processedAt": datetime.utcnow(),
                }
            },
        )
        # Return HTTP 200 to avoid provider retries while preserving the failure for diagnostics.
        return {"message": "Callback captured but processing failed", "eventId": event_id}


def _is_success_from_status_object(status_obj: dict) -> bool | None:
    """Infer success/failure from provider status object when present."""
    if not isinstance(status_obj, dict):
        return None

    success_flag = status_obj.get("success")
    if isinstance(success_flag, bool):
        return success_flag

    code = str(status_obj.get("code", "")).strip()
    result_code = str(status_obj.get("result_code", "")).strip()
    response_code = str(status_obj.get("response_code", "")).strip().upper()

    if code in {"200", "201", "00"}:
        return True
    if result_code in {"0", "00", "000000", "SUCCESS"}:
        return True
    if code and code not in {"200", "201", "00"}:
        return False
    if response_code.startswith("DP"):
        return False
    return None


def _extract_status_and_success(payload: dict, tx: dict) -> tuple[str, bool, str | None]:
    """Return normalized status text, success flag, and best-effort failure reason."""
    status_obj = payload.get("status") if isinstance(payload.get("status"), dict) else None
    status_from_obj = _is_success_from_status_object(status_obj) if status_obj else None

    raw_values = [
        tx.get("status"),
        tx.get("transactionStatus"),
        tx.get("state"),
        payload.get("status") if not isinstance(payload.get("status"), dict) else None,
        payload.get("transactionStatus"),
        payload.get("state"),
        payload.get("result"),
    ]
    joined = " ".join(str(v).strip().upper() for v in raw_values if v is not None and str(v).strip())

    reason = (
        tx.get("message")
        or payload.get("message")
        or (status_obj.get("message") if status_obj else None)
        or None
    )

    success_words = ["SUCCESS", "SUCCESSFUL", "COMPLETED", "TS", "OK", "APPROVED", "PAID"]
    failure_words = ["FAILED", "FAIL", "ERROR", "REJECT", "DECLINED", "CANCEL", "TIMEOUT"]

    if status_from_obj is True:
        return (joined or "SUCCESS", True, reason)
    if status_from_obj is False:
        return (joined or "FAILED", False, reason)

    if any(word in joined for word in success_words):
        return (joined or "SUCCESS", True, reason)
    if any(word in joined for word in failure_words):
        return (joined or "FAILED", False, reason)

    # Default to failure-safe for unknown states to avoid silent false completion.
    return (joined or "UNKNOWN", False, reason)


def _has_reconcile_evidence(report: dict[str, Any] | None) -> bool:
    if not isinstance(report, dict):
        return False

    # Require at least one non-empty provider trace field for manual completion.
    evidence_keys = [
        "provider_tx_id",
        "receipt",
        "result_code",
        "status",
        "message",
        "airtel_reference",
        "operator_note",
    ]
    return any(str(report.get(k, "")).strip() for k in evidence_keys)


def _resolve_airtel_callback_base_url() -> str:
    """Resolve callback base URL from either modern or legacy env keys."""
    base = os.environ.get("AIRTEL_CALLBACK_BASE_URL", "").strip()
    if base:
        return base.rstrip("/")

    legacy = os.environ.get("AIRTEL_GATEWAY_CALLBACK_URL", "").strip()
    if not legacy:
        return ""

    # Accept a full callback URL like .../api/v1/callbacks/collections and reduce to base domain.
    marker = "/api/v1/callbacks"
    idx = legacy.find(marker)
    if idx > 0:
        legacy = legacy[:idx]

    return legacy.rstrip("/")

@router.post("/execute", status_code=201)
async def execute_ramp(body: RampExecute, db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id_raw = current_user["_id"]
    user_id = safe_object_id(user_id_raw)
    user_id_candidates = build_user_id_candidates(user_id_raw)
    trade_id = f"TRADE_{uuid.uuid4().hex[:8].upper()}"
    provider_reference = f"LIVE-{uuid.uuid4().hex[:8].upper()}"

    receive = body.amount
    live_swap_quote = None

    # 🧮 UNIVERSAL SPREAD PROFIT CALCULATOR (Handles all 15 assets)
    profit_amount = 0.0
    profit_currency = body.from_asset

    if body.direction == "swap":
        rate_book = await get_or_create_rate_book(db)
        if not bool(rate_book.get("active", True)):
            raise HTTPException(status_code=503, detail="Trading is currently paused by treasury.")

        live_swap_quote = compute_swap_quote_from_book(body.from_asset, body.to_asset, body.amount, rate_book)
        receive = live_swap_quote["receive_amount"]
        profit_amount = live_swap_quote["fee_amount"]
        profit_currency = body.to_asset

    # ========================================================
    # 💰 LOG COMPANY SPREAD & CORPORATE REVENUE ACCUMULATION
    # ========================================================
    if profit_amount > 0:
        settlement_doc = {
            "_id": f"PNL_{uuid.uuid4().hex[:6].upper()}",
            "desc": f"{body.amount} {body.from_asset} → {receive} {body.to_asset} ({body.channel})",
            "profit_amount": round(profit_amount, 4),
            "profit_currency": profit_currency,
            "channel": body.channel,
            "timestamp": datetime.utcnow(),
            "status": "COMPLETED",
            "trade_id": trade_id
        }
        await db["settlement_logs"].insert_one(settlement_doc)
        print(f"💰 Mamlaka Captured Spread Profit via [{body.channel}]: +{round(profit_amount, 4)} {profit_currency}")

        # 🟢 CORPORATE REVENUE ACCUMULATION
        await db["company_revenue"].update_one(
            {"_id": "corporate_treasury"},
            {"$inc": {profit_currency: profit_amount}},
            upsert=True
        )
        print(f"🏦 Profit safely locked in Corporate Revenue Wallet.")

    # ========================================================
    # INTERNAL SWAP EXECUTION (User Ledger Transfer)
    # ========================================================
    if body.direction == "swap":
        receive_amount = float(receive)
        
        # Lock & Verify Balance
        wallet = await db["retail_wallets"].find_one({"userId": {"$in": user_id_candidates}})
        current_balance = float(wallet.get(body.from_asset, 0)) if wallet else 0
        
        if current_balance < body.amount:
            raise HTTPException(status_code=400, detail=f"Insufficient {body.from_asset} balance. You only have {current_balance}.")

        # Atomic Database Update
        wallet_owner_id = wallet.get("userId") if wallet else user_id
        await db["retail_wallets"].update_one(
            {"userId": wallet_owner_id},
            {
                "$inc": {
                    body.from_asset: -body.amount,
                    body.to_asset: receive_amount
                }
            }
        )

        # Log User Receipt
        history_doc = {
            "_id": trade_id,
            "direction": "swap",
            "channel": body.channel or "Internal Ledger",
            "fromAsset": body.from_asset,
            "toAsset": body.to_asset,
            "fromAmount": body.amount,
            "toAmount": receive_amount,
            "rate": live_swap_quote["execution_rate"] if live_swap_quote else body.rate,
            "marketRate": live_swap_quote["market_rate"] if live_swap_quote else None,
            "fee": live_swap_quote["fee_amount"] if live_swap_quote else body.fee,
            "status": "completed",
            "userId": wallet_owner_id,
            "date": datetime.utcnow().strftime("%b %d, %Y"),
            "timeAgo": "Just now",
            "createdAt": datetime.utcnow() 
        }
        await db["ramp_entries"].insert_one(history_doc)
        
        return {
            "id": trade_id,
            "status": "completed",
            "message": "Swap executed instantly.",
            "receive": receive_amount
        }

    # ========================================================
    # ON-RAMP (DEPOSIT KES VIA AIRTEL STK PUSH)
    # ========================================================
    if body.direction == "on" and body.channel == "Mobile Money":
        # 1. Sanitize Airtel MSISDN into the local provider format accepted by the Go gateway.
        #    Provider examples in latest evidence: 731740909 (9-digit local format), not E.164 254...
        phone = str(body.counterparty).strip()
        phone = phone.replace(' ', '').replace('-', '')

        if phone.startswith('+'):
            phone = phone[1:]

        # Accept both 2547... and 07... and 7... forms and collapse them to provider-local 7XXXXXXXX.
        if phone.startswith('254'):
            phone = phone[3:]
        if phone.startswith('0'):
            phone = phone[1:]

        is_airtel_prefix = phone.startswith(("73", "75", "78", "10"))
        if len(phone) != 9 or not is_airtel_prefix:
            raise HTTPException(
                status_code=400,
                detail="Unsupported MSISDN for Airtel STK. Use an Airtel Money number (07XXXXXXXX / 2547XXXXXXXX).",
            )

        # 2. Retrieve Gateway details from environment variables
        gateway_url = os.environ.get("AIRTEL_GATEWAY_URL", "https://airtime.mamlakapsp.com")
        api_key = os.environ.get("AIRTEL_GATEWAY_API_KEY", "")
        gateway_body: dict[str, Any] = {}
        gateway_ack_id: str | None = None
        gateway_status_message: str | None = None
        gateway_http_status: int | None = None
        gateway_response_text: str | None = None
        e164_phone = f"254{phone}"
        national_phone = f"0{phone}"

        try:
            headers = {
                "X-API-Key": api_key,
                "Content-Type": "application/json"
            }

            callback_base_url = _resolve_airtel_callback_base_url()
            if not callback_base_url:
                raise HTTPException(
                    status_code=503,
                    detail="AIRTEL_CALLBACK_BASE_URL (or AIRTEL_GATEWAY_CALLBACK_URL) is not configured. Callback delivery cannot be guaranteed.",
                )

            payload = {
                "phone_number": phone,
                "amount": int(body.amount),
                "reference": provider_reference
            }
            # Send multiple phone fields to support gateway implementations with different contracts.
            payload["msisdn"] = e164_phone
            payload["phone"] = national_phone
            collection_callback = f"{callback_base_url}/api/v1/callbacks/collections"
            # Support both snake_case and camelCase contracts depending on gateway implementation.
            payload["callback_url"] = collection_callback
            payload["callbackUrl"] = collection_callback

            # 3. Asynchronously trigger STK push on the Airtel Go Gateway
            async with httpx.AsyncClient() as client:
                stk_url = f"{gateway_url}/api/v1/stk/push"
                response = await client.post(stk_url, json=payload, headers=headers, timeout=15.0)
                gateway_http_status = response.status_code
                gateway_response_text = response.text[:4000]
                if response.is_error:
                    print(f"❌ Airtel Gateway HTTP {response.status_code}: {response.text}")
                    raise HTTPException(
                        status_code=502,
                        detail=f"Airtel gateway returned {response.status_code}: {response.text}"
                    )
                response.raise_for_status()

                # Some gateways return HTTP 200 but business failure in body.status.success=false.
                try:
                    gateway_body = response.json()
                except Exception:
                    gateway_body = {}

                if isinstance(gateway_body, dict):
                    gateway_ack_id = (
                        gateway_body.get("reference")
                        or gateway_body.get("request_id")
                        or gateway_body.get("requestId")
                        or gateway_body.get("transactionReference")
                    )

                status_obj = gateway_body.get("status") if isinstance(gateway_body, dict) else None
                if isinstance(status_obj, dict):
                    gateway_status_message = str(
                        status_obj.get("message")
                        or status_obj.get("description")
                        or ""
                    ).strip() or None
                    status_success = _is_success_from_status_object(status_obj)
                    if status_success is False:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Airtel gateway business rejection: {status_obj}"
                        )

                # Some implementations return business flags at the root instead of status object.
                if isinstance(gateway_body, dict):
                    root_success = gateway_body.get("success")
                    root_code = str(gateway_body.get("code", "")).strip()
                    root_result_code = str(gateway_body.get("result_code", "")).strip()
                    root_message = str(
                        gateway_body.get("message")
                        or gateway_body.get("description")
                        or ""
                    ).strip()
                    root_text = f"{root_code} {root_result_code} {root_message}".upper()

                    explicit_root_failure = (
                        root_success is False
                        or (root_code and root_code not in {"200", "201", "00"})
                        or (root_result_code and root_result_code not in {"0", "00", "000000", "SUCCESS"})
                        or any(k in root_text for k in ["FAILED", "REJECT", "DECLINED", "ERROR", "INSUFFICIENT", "TIMEOUT"])
                    )

                    if explicit_root_failure:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Airtel gateway business rejection: {gateway_body}"
                        )

        except HTTPException:
            raise
        except Exception as e:
            print(f"❌ Airtel Gateway Error: {str(e)}")
            raise HTTPException(
                status_code=502,
                detail=f"Airtel STK gateway rejected the deposit request: {str(e)}"
            )

        # 4. Save transaction entry into MongoDB Atlas only after the gateway accepted the request
        doc = {
            "_id": trade_id,
            "direction": body.direction,
            "channel": body.channel,
            "fromAsset": body.from_asset,
            "toAsset": body.to_asset,
            "fromAmount": body.amount,
            "toAmount": receive,
            "status": "processing",
            "userId": user_id,
            "phone": phone,
            "phoneE164": e164_phone,
            "phoneNational": national_phone,
            "providerReference": provider_reference,
            "reference": provider_reference,
            "callbackUrl": collection_callback,
            "gatewayAckId": gateway_ack_id,
            "gatewayStatusMessage": gateway_status_message,
            "gatewayHttpStatus": gateway_http_status,
            "gatewayResponseText": gateway_response_text,
            "gatewayResponse": gateway_body,
            "date": datetime.utcnow().strftime("%b %d, %Y"),
            "timeAgo": datetime.utcnow().strftime("%H:%M:%S"),
            "createdAt": datetime.utcnow()
        }
        await db["ramp_entries"].insert_one(doc)

        response_payload = {
            "id": trade_id,
            "status": "processing",
            "message": "Airtel STK request sent to provider.",
            "provider": "Airtel",
            "providerReference": provider_reference,
            "callbackUrl": collection_callback,
            "recipient": f"0{phone}",
            "providerAcknowledged": True,
            "nextStep": "If no prompt arrives within 60 seconds, retry once or confirm the handset has network and STK enabled.",
        }
        if gateway_ack_id:
            response_payload["gatewayAckId"] = str(gateway_ack_id)
        if gateway_status_message:
            response_payload["gatewayStatusMessage"] = gateway_status_message

        return response_payload

    # ========================================================
    # Legacy STK deposit (M-Pesa) kept as comment for later reuse
    # ========================================================
    # if body.direction == "on" and body.channel == "Mobile Money":
    #     try:
    #         token = mam_laka.get_access_token()
    #         initiate_url = f"{mam_laka.base_url}/api/v1/mobile/initiate"
    #         headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    #         payload = {
    #             "impalaMerchantId": "meshex_sandbox",
    #             "displayName": "Mamlaka Deposit",
    #             "currency": "KES",
    #             "amount": int(body.amount),
    #             "payerPhone": body.counterparty,
    #             "mobileMoneySP": "M-Pesa",
    #             "externalId": trade_id,
    #             "callbackUrl": "https://hemathermal-ha-dextrously.ngrok-free.dev/api/ramp/b2c/result"
    #         }
    #         requests.post(initiate_url, json=payload, headers=headers, timeout=15)
    #     except Exception as e:
    #         print(f"❌ STK Error: {str(e)}")
    #
    #     doc = {
    #         "_id": trade_id, "direction": body.direction, "channel": body.channel,
    #         "fromAsset": body.from_asset, "toAsset": body.to_asset,
    #         "fromAmount": body.amount, "toAmount": receive,
    #         "status": "processing", "userId": user_id,
    #         "date": datetime.utcnow().strftime("%b %d, %Y"),
    #         "timeAgo": datetime.utcnow().strftime("%H:%M:%S"),
    #         "createdAt": datetime.utcnow()
    #     }
    #     await db["ramp_entries"].insert_one(doc)
    #     return {"id": trade_id, "status": "processing...", "message": "STK Push sent!"}

    
    # ========================================================
    # Legacy Safaricom / M-Pesa STK off-ramp kept as comment for later reuse
    # ========================================================
    # if body.direction == "off" and body.channel == "Mobile Money":
    #     # 1. Verify User's Internal KES Balance
    #     user_wallet = await db["retail_wallets"].find_one({"userId": user_id})
    #     current_kes = float(user_wallet.get("KES", 0.0)) if user_wallet else 0.0
    #
    #     if current_kes < body.amount:
    #         raise HTTPException(status_code=400, detail=f"Insufficient KES balance. You have {current_kes} KES.")
    #
    #     # 2. Lock/Deduct Funds from Internal Wallet
    #     await db["retail_wallets"].update_one(
    #         {"userId": user_id},
    #         {"$inc": {"KES": -body.amount}}
    #     )
    #
    #     try:
    #         token = mam_laka.get_access_token()
    #         payout_url = f"{mam_laka.base_url}/api/v1/mobile/transfer"
    #         headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    #         payload = {
    #             "impalaMerchantId": "meshex_sandbox",
    #             "currency": "KES",
    #             "amount": int(receive),
    #             "recipientPhone": body.counterparty,
    #             "mobileMoneySP": "M-Pesa",
    #             "externalId": trade_id,
    #             "callbackUrl": "https://hemathermal-ha-dextrously.ngrok-free.dev/api/ramp/b2c/result"
    #         }
    #         requests.post(payout_url, json=payload, headers=headers, timeout=15)
    #     except Exception as e:
    #         print(f"B2C Error: {e}")
    #         await db["retail_wallets"].update_one({"userId": user_id}, {"$inc": {"KES": body.amount}})
    #         raise HTTPException(status_code=502, detail="Failed to connect to Mam-laka API. Funds refunded.")
    #
    #     doc = {
    #         "_id": trade_id, "direction": body.direction, "channel": body.channel,
    #         "fromAsset": body.from_asset, "toAsset": body.to_asset,
    #         "fromAmount": body.amount, "toAmount": receive,
    #         "status": "processing", "userId": user_id,
    #         "date": datetime.utcnow().strftime("%b %d, %Y"),
    #         "timeAgo": datetime.utcnow().strftime("%H:%M:%S"),
    #         "createdAt": datetime.utcnow()
    #     }
    #     await db["ramp_entries"].insert_one(doc)
    #     return {"id": trade_id, "receive": receive, "status": "processing", "message": "Withdrawal sent to M-Pesa!"}

    # ========================================================
    # OFF-RAMP (WITHDRAW TO AIRTEL MONEY VIA GATEWAY)
    # ========================================================
    if body.direction == "off" and body.channel == "Mobile Money":
        # 1. Verify User's Internal KES Balance
        user_wallet = await db["retail_wallets"].find_one({"userId": {"$in": user_id_candidates}})
        current_kes = float(user_wallet.get("KES", 0.0)) if user_wallet else 0.0

        if current_kes < body.amount:
            raise HTTPException(status_code=400, detail=f"Insufficient KES balance. You have {current_kes} KES.")

        # 2. Lock/Deduct Funds from Internal Wallet (Optimistic deduction)
        wallet_owner_id = user_wallet.get("userId") if user_wallet else user_id
        await db["retail_wallets"].update_one(
            {"userId": wallet_owner_id},
            {"$inc": {"KES": -body.amount}}
        )

        try:
            # --- AIRTEL GO GATEWAY INTEGRATION ---
            payout_url = f"{os.environ.get('AIRTEL_GATEWAY_URL', 'https://airtime.mamlakapsp.com')}/api/v1/disburse"

            # Airtel disburse requires strictly alphanumeric reference (no hyphens/special chars).
            alphanumeric_ref = f"B2C{uuid.uuid4().hex[:20].upper()}"

            # Normalize Airtel phone to the provider contract: 9 digits local form
            phone_for_airtel = str(body.counterparty).strip().replace(' ', '').replace('-', '')
            if phone_for_airtel.startswith('+'):
                phone_for_airtel = phone_for_airtel[1:]
            if phone_for_airtel.startswith('254'):
                phone_for_airtel = phone_for_airtel[3:]
            if phone_for_airtel.startswith('0'):
                phone_for_airtel = phone_for_airtel[1:]

            is_airtel_prefix = phone_for_airtel.startswith(("73", "75", "78", "10"))
            if len(phone_for_airtel) != 9 or not is_airtel_prefix:
                raise HTTPException(
                    status_code=400,
                    detail="Unsupported MSISDN for Airtel payout. Use an Airtel Money number (07XXXXXXXX / 2547XXXXXXXX).",
                )

            payload = {
                "phone_number": phone_for_airtel,
                "amount": int(receive),
                "reference": alphanumeric_ref
            }

            callback_base_url = _resolve_airtel_callback_base_url()
            if not callback_base_url:
                raise HTTPException(
                    status_code=503,
                    detail="AIRTEL_CALLBACK_BASE_URL (or AIRTEL_GATEWAY_CALLBACK_URL) is not configured. Callback delivery cannot be guaranteed.",
                )

            disburse_callback = f"{callback_base_url}/api/v1/callbacks/disbursements"
            payload["callback_url"] = disburse_callback
            payload["callbackUrl"] = disburse_callback

            gateway_api_key = os.environ.get("AIRTEL_GATEWAY_API_KEY", "")
            headers = {
                "X-API-Key": gateway_api_key,
                "Content-Type": "application/json"
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(payout_url, json=payload, headers=headers, timeout=15.0)
                if response.is_error:
                    response_text = response.text
                    print(f"Airtel Gateway Rejected: {response_text}")
                    await db["retail_wallets"].update_one({"userId": wallet_owner_id}, {"$inc": {"KES": body.amount}})
                    raise HTTPException(status_code=400, detail=response_text)
                response.raise_for_status()

                # Some gateways return HTTP 200 but business failure in body.status.success=false.
                try:
                    gateway_body = response.json()
                except Exception:
                    gateway_body = {}
                status_obj = gateway_body.get("status") if isinstance(gateway_body, dict) else None
                if isinstance(status_obj, dict):
                    status_success = _is_success_from_status_object(status_obj)
                    if status_success is False:
                        response_text = response.text
                        await db["retail_wallets"].update_one({"userId": wallet_owner_id}, {"$inc": {"KES": body.amount}})
                        raise HTTPException(status_code=400, detail=f"Airtel gateway business rejection: {response_text}")

        except HTTPException:
            raise
        except Exception as e:
            print(f"Gateway Connection Error: {e}")
            await db["retail_wallets"].update_one({"userId": wallet_owner_id}, {"$inc": {"KES": body.amount}})
            raise HTTPException(status_code=502, detail="Failed to connect to Airtel Gateway. Funds refunded.")

        # 4. Log the Transaction (Only runs if Airtel accepts the B2C request)
        doc = {
            "_id": trade_id,
            "direction": body.direction,
            "channel": body.channel,
            "fromAsset": body.from_asset,
            "toAsset": body.to_asset,
            "fromAmount": body.amount,
            "toAmount": receive,
            "status": "processing",
            "userId": wallet_owner_id,
            "providerReference": alphanumeric_ref,
            "date": datetime.utcnow().strftime("%b %d, %Y"),
            "timeAgo": "Just now",
            "createdAt": datetime.utcnow()
        }
        await db["ramp_entries"].insert_one(doc)

        return {
            "id": trade_id,
            "receive": receive,
            "status": "processing",
            "message": "Withdrawal sent to Airtel Money!"
        }

    return {"id": trade_id, "receive": receive, "status": "processing"}
        
"""
# ========================================================
# 🟢 SECURE WEBHOOK RECEIVER (ROBUST VERSION)
# ========================================================
@router.post("/b2c/result") 
async def mamlaka_stk_callback(payload: dict, db=Depends(get_db)):
    # 1. LOG RAW PAYLOAD (Crucial for debugging African APIs)
    print(f"📦 [WEBHOOK INCOMING] Raw Payload: {payload}")
    
    # 2. CHECK MULTIPLE POSSIBLE KEYS FOR THE STATUS
    # Different African gateways use different keys (status, transactionStatus, state)
    tx_status = str(payload.get("transactionStatus", "")).strip().upper()
    tx_state = str(payload.get("status", "")).strip().upper()
    tx_report = str(payload.get("transactionReport", "")).strip().upper()
    tx_message = str(payload.get("message", "")).strip().upper()

    # 3. SMART SUCCESS DETECTION
    # If ANY of the keys contain "SUCCESS" or the report says "PROCESSED SUCCESSFULLY", it's a win!
    is_success = (
        "SUCCESS" in tx_status or 
        "SUCCESS" in tx_state or 
        "COMPLETED" in tx_state or
        "PROCESSED SUCCESSFULLY" in tx_report or 
        "SUCCESS" in tx_message
    )

    external_id = payload.get("externalId", "") 
    
    if not external_id:
        return {"status": "ignored"}

    if external_id.startswith("TRADE_") or external_id.startswith("SWEEP_") or external_id.startswith("REV_"):
        trade = await db["ramp_entries"].find_one({"_id": external_id})
        
        if not trade:
            print(f"⚠️ [WEBHOOK] Trade {external_id} not found in database.")
            return {"status": "ignored"}

        # Prevent double-processing if M-Pesa sends the webhook twice
        if trade.get("status") not in ["processing", "pending"]:
            print(f"⏭️ [WEBHOOK] Trade {external_id} already processed ({trade.get('status')}). Ignoring duplicate.")
            return {"status": "already_processed"}
            
        # --- HANDLE SUCCESS ---
        if is_success:
            await db["ramp_entries"].update_one(
                {"_id": external_id}, 
                {"$set": {"status": "completed", "report": payload}}
            )
            
            if trade.get("direction") == "on":
                user_id = trade.get("userId")
                asset = trade.get("fromAsset", "KES")
                amount = float(trade.get("fromAmount", 0))
                
                await db["retail_wallets"].update_one(
                    {"userId": user_id},
                    {"$inc": {asset: amount}},
                    upsert=True
                )
                print(f"🟢 [WEBHOOK] SUCCESS! Credited {amount} {asset} to user {user_id}")
                
        # --- HANDLE FAILURE ---
        else:
            await db["ramp_entries"].update_one(
                {"_id": external_id}, 
                {"$set": {"status": "failed", "report": payload}}
            )
            print(f"🔴 [WEBHOOK] FAILED! Trade {external_id} rejected. Full payload saved to DB.")
            
            # If it was a withdrawal, give the money back
            if trade.get("direction") == "off":
                user_id = trade.get("userId")
                asset = trade.get("fromAsset", "KES")
                amount = float(trade.get("fromAmount", 0))
                await db["retail_wallets"].update_one(
                    {"userId": user_id}, 
                    {"$inc": {asset: amount}}
                )
                print(f"🔄 [WEBHOOK] Refunded {amount} {asset} back to user {user_id}")

    return {"status": "acknowledged"}

    """   
@router.get("/history")
async def get_ramp_history(db=Depends(get_db), current_user=Depends(get_current_user)):
    user_ids = build_user_id_candidates(current_user["_id"])
    cursor = db["ramp_entries"].find({"userId": {"$in": user_ids}}).sort("createdAt", -1).limit(50)
    entries = await cursor.to_list(length=50)

    formatted_entries = []
    for e in entries:
        formatted_entries.append({
            "id": str(e["_id"]), 
            "direction": e.get("direction", "on"),
            "channel": e.get("channel", "Mobile Money"), 
            "fromAsset": e.get("fromAsset", "KES"),
            "toAsset": e.get("toAsset", "USDA"), 
            "fromAmount": e.get("fromAmount", 0),
            "toAmount": e.get("toAmount", 0), 
            "status": e.get("status", "completed"),
            "date": e.get("date", "Today"), 
            "timeAgo": e.get("timeAgo", "Recently"),
            # Send proper ISO timestamp to the frontend
            "createdAt": e.get("createdAt", datetime.utcnow()).isoformat() + "Z" if e.get("createdAt") else None
        })
    return {"status": "success", "entries": formatted_entries}


@router.get("/stk/latest")
async def get_latest_stk_dispatch(
    phone: str | None = None,
    reference: str | None = None,
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    user_ids = build_user_id_candidates(current_user["_id"])

    query: dict[str, Any] = {
        "direction": "on",
        "channel": "Mobile Money",
        "userId": {"$in": user_ids},
    }

    normalized_phone = None
    if phone:
        normalized_phone = str(phone).strip().replace(" ", "").replace("-", "")
        if normalized_phone.startswith("+"):
            normalized_phone = normalized_phone[1:]
        if normalized_phone.startswith("254"):
            normalized_phone = normalized_phone[3:]
        if normalized_phone.startswith("0"):
            normalized_phone = normalized_phone[1:]

    and_filters: list[dict[str, Any]] = []
    if reference and str(reference).strip():
        ref_value = str(reference).strip()
        and_filters.append({"$or": [{"providerReference": ref_value}, {"reference": ref_value}, {"_id": ref_value}]})
    if normalized_phone:
        and_filters.append({"$or": [{"phone": normalized_phone}, {"phoneNational": f"0{normalized_phone}"}, {"phoneE164": f"254{normalized_phone}"}]})

    if and_filters:
        query["$and"] = and_filters

    entry = await db["ramp_entries"].find_one(query, sort=[("createdAt", -1)])
    if not entry:
        return {
            "status": "not_found",
            "message": "No matching STK dispatch found for the supplied filters.",
            "filters": {"phone": phone, "reference": reference},
        }

    return {
        "status": "ok",
        "dispatch": {
            "id": str(entry.get("_id")),
            "createdAt": entry.get("createdAt").isoformat() + "Z" if entry.get("createdAt") else None,
            "phone": entry.get("phoneNational") or entry.get("phone") or phone,
            "providerReference": entry.get("providerReference"),
            "callbackUrl": entry.get("callbackUrl"),
            "gatewayAckId": entry.get("gatewayAckId"),
            "gatewayStatusMessage": entry.get("gatewayStatusMessage"),
            "gatewayHttpStatus": entry.get("gatewayHttpStatus"),
            "gatewayResponseText": entry.get("gatewayResponseText"),
            "status": entry.get("status"),
            "providerStatus": entry.get("providerStatus"),
            "errorReason": entry.get("error_reason"),
        },
    }


@router.post("/c2b/result")
async def airtel_gateway_collections_relay(request: Request, db=Depends(get_db)):
    """Compatibility route for the gateway's existing collection relay target."""
    return await _handle_airtel_callback(request, db, "gateway_collections_relay")


@router.post("/b2c/result")
async def airtel_gateway_disbursements_relay(request: Request, db=Depends(get_db)):
    """Compatibility route for the gateway's existing disbursement relay target."""
    return await _handle_airtel_callback(request, db, "gateway_disbursements_relay")


@router.get("/callbacks/events")
async def get_airtel_callback_events(
    limit: int = 30,
    db=Depends(get_db),
    current_user=Depends(get_current_user_with_role),
):
    if not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Admin role required.")

    safe_limit = max(1, min(int(limit or 30), 200))
    cursor = db["airtel_callback_events"].find({}).sort("receivedAt", -1).limit(safe_limit)
    events = await cursor.to_list(length=safe_limit)

    rows = []
    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        tx = payload.get("transaction") if isinstance(payload.get("transaction"), dict) else {}
        ref = _extract_reference_from_payload(payload, tx)
        rows.append({
            "id": str(event.get("_id")),
            "source": event.get("source"),
            "path": event.get("path"),
            "processed": bool(event.get("processed")),
            "reference": ref,
            "error": event.get("error"),
            "result": event.get("result"),
            "receivedAt": event.get("receivedAt").isoformat() + "Z" if event.get("receivedAt") else None,
        })

    return {"status": "ok", "count": len(rows), "events": rows}


@router.post("/reconcile/deposits")
async def reconcile_processing_deposits(
    body: ReconcileDepositsRequest,
    db=Depends(get_db),
    current_user=Depends(get_current_user_with_role),
):
    if not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Admin role required for manual reconciliation.")

    user_ids = build_user_id_candidates(current_user["_id"])

    refs = [str(r).strip() for r in body.references if str(r).strip()]
    refs = list(dict.fromkeys(refs))
    if not refs:
        raise HTTPException(status_code=400, detail="No valid references provided.")

    desired = str(body.result_status or "completed").strip().lower()
    if desired not in ["completed", "failed"]:
        raise HTTPException(status_code=400, detail="result_status must be 'completed' or 'failed'.")

    if desired == "completed":
        if not body.confirm_paid:
            raise HTTPException(status_code=400, detail="Manual completion requires confirm_paid=true.")
        if not _has_reconcile_evidence(body.provider_report):
            raise HTTPException(status_code=400, detail="Manual completion requires provider_report evidence.")

    cursor = db["ramp_entries"].find({
        "direction": "on",
        "status": {"$in": ["processing", "pending"]},
        "$or": [
            {"providerReference": {"$in": refs}},
            {"_id": {"$in": refs}},
        ],
    })
    entries = await cursor.to_list(length=500)

    if not entries:
        return {
            "status": "ok",
            "message": "No matching processing deposits found for this account.",
            "requested": refs,
            "credited": [],
            "skipped": refs,
        }

    credited = []
    skipped = []
    failed = []

    for entry in entries:
        entry_id = str(entry.get("_id"))
        ref = str(entry.get("providerReference") or entry_id)

        claim = await db["ramp_entries"].update_one(
            {"_id": entry.get("_id"), "status": {"$in": ["processing", "pending"]}},
            {
                "$set": {
                    "status": "crediting",
                    "reconcileRequestedAt": datetime.utcnow(),
                }
            },
        )
        if claim.modified_count == 0:
            skipped.append(ref)
            continue

        wallet_asset = entry.get("fromAsset") or "KES"
        amount = float(entry.get("toAmount") or entry.get("fromAmount") or 0)
        wallet_user_id = entry.get("userId")

        # Guardrails: non-admin users are blocked above; also ensure we don't reconcile other users accidentally.
        if not any(str(wallet_user_id) == str(uid) for uid in user_ids):
            await db["ramp_entries"].update_one(
                {"_id": entry.get("_id")},
                {"$set": {"status": "processing", "updatedAt": datetime.utcnow()}},
            )
            skipped.append(ref)
            continue

        if amount <= 0:
            await db["ramp_entries"].update_one(
                {"_id": entry.get("_id")},
                {
                    "$set": {
                        "status": "failed",
                        "error_reason": "Invalid amount on reconciled deposit.",
                        "updatedAt": datetime.utcnow(),
                    }
                },
            )
            failed.append({"reference": ref, "reason": "invalid_amount"})
            continue

        try:
            # Credit wallet only when explicitly completing a verified deposit.
            if desired == "completed":
                await db["retail_wallets"].update_one(
                    {"userId": wallet_user_id},
                    {"$inc": {wallet_asset: amount}},
                    upsert=True,
                )

            await db["ramp_entries"].update_one(
                {"_id": entry.get("_id")},
                {
                    "$set": {
                        "status": desired,
                        "updatedAt": datetime.utcnow(),
                        "providerStatus": "MANUAL_RECONCILE",
                        "providerReport": body.provider_report or {"source": "manual_reconcile"},
                        "reconciledBy": str(current_user.get("_id")),
                        "error_reason": "Manual reconcile marked failed" if desired == "failed" else None,
                    }
                },
            )
            credited.append({
                "reference": ref,
                "entryId": entry_id,
                "asset": wallet_asset,
                "amount": amount,
                "status": desired,
            })
        except Exception as exc:
            await db["ramp_entries"].update_one(
                {"_id": entry.get("_id")},
                {
                    "$set": {
                        "status": "processing",
                        "reconcileError": str(exc),
                        "updatedAt": datetime.utcnow(),
                    }
                },
            )
            failed.append({"reference": ref, "reason": str(exc)})

    unresolved = [r for r in refs if r not in {c["reference"] for c in credited} and r not in skipped]
    skipped.extend(unresolved)

    return {
        "status": "ok",
        "requested": refs,
        "credited": credited,
        "skipped": skipped,
        "failed": failed,
    }


@router.post("/reconcile/withdrawals")
async def reconcile_processing_withdrawals(
    body: ReconcileWithdrawalsRequest,
    db=Depends(get_db),
    current_user=Depends(get_current_user_with_role),
):
    if not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Admin role required for manual reconciliation.")

    user_ids = build_user_id_candidates(current_user["_id"])

    refs = [str(r).strip() for r in body.references if str(r).strip()]
    refs = list(dict.fromkeys(refs))
    if not refs:
        raise HTTPException(status_code=400, detail="No valid references provided.")

    desired = str(body.result_status or "completed").strip().lower()
    if desired not in ["completed", "failed"]:
        raise HTTPException(status_code=400, detail="result_status must be 'completed' or 'failed'.")

    if desired == "completed":
        if not body.confirm_paid:
            raise HTTPException(status_code=400, detail="Manual completion requires confirm_paid=true.")
        if not _has_reconcile_evidence(body.provider_report):
            raise HTTPException(status_code=400, detail="Manual completion requires provider_report evidence.")

    cursor = db["ramp_entries"].find({
        "direction": "off",
        "status": {"$in": ["processing", "pending"]},
        "$or": [
            {"providerReference": {"$in": refs}},
            {"_id": {"$in": refs}},
        ],
    })
    entries = await cursor.to_list(length=500)

    if not entries:
        return {
            "status": "ok",
            "message": "No matching processing withdrawals found for this account.",
            "requested": refs,
            "updated": [],
            "skipped": refs,
            "failed": [],
        }

    updated = []
    skipped = []
    failed = []

    for entry in entries:
        entry_id = str(entry.get("_id"))
        ref = str(entry.get("providerReference") or entry_id)

        claim = await db["ramp_entries"].update_one(
            {"_id": entry.get("_id"), "status": {"$in": ["processing", "pending"]}},
            {
                "$set": {
                    "status": "reconciling",
                    "reconcileRequestedAt": datetime.utcnow(),
                }
            },
        )
        if claim.modified_count == 0:
            skipped.append(ref)
            continue

        wallet_asset = entry.get("fromAsset") or "KES"
        amount = float(entry.get("fromAmount") or entry.get("toAmount") or 0)
        wallet_user_id = entry.get("userId")

        if not any(str(wallet_user_id) == str(uid) for uid in user_ids):
            await db["ramp_entries"].update_one(
                {"_id": entry.get("_id")},
                {"$set": {"status": "processing", "updatedAt": datetime.utcnow()}},
            )
            skipped.append(ref)
            continue

        try:
            # Refund wallet only when marking failed.
            if desired == "failed" and amount > 0:
                await db["retail_wallets"].update_one(
                    {"userId": wallet_user_id},
                    {"$inc": {wallet_asset: amount}},
                    upsert=True,
                )

            await db["ramp_entries"].update_one(
                {"_id": entry.get("_id")},
                {
                    "$set": {
                        "status": desired,
                        "updatedAt": datetime.utcnow(),
                        "providerStatus": "MANUAL_RECONCILE",
                        "providerReport": body.provider_report or {"source": "manual_withdraw_reconcile"},
                        "reconciledBy": str(current_user.get("_id")),
                        "error_reason": "Manual reconcile marked failed" if desired == "failed" else None,
                    }
                },
            )
            updated.append({
                "reference": ref,
                "entryId": entry_id,
                "status": desired,
                "refunded": desired == "failed",
                "asset": wallet_asset,
                "amount": amount,
            })
        except Exception as exc:
            await db["ramp_entries"].update_one(
                {"_id": entry.get("_id")},
                {
                    "$set": {
                        "status": "processing",
                        "reconcileError": str(exc),
                        "updatedAt": datetime.utcnow(),
                    }
                },
            )
            failed.append({"reference": ref, "reason": str(exc)})

    unresolved = [r for r in refs if r not in {u["reference"] for u in updated} and r not in skipped]
    skipped.extend(unresolved)

    return {
        "status": "ok",
        "requested": refs,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
    }


@router.post("/reconcile/withdrawals/correct")
async def correct_completed_withdrawals(
    body: CorrectCompletedWithdrawalsRequest,
    db=Depends(get_db),
    current_user=Depends(get_current_user_with_role),
):
    if not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Admin role required for correction.")

    refs = [str(r).strip() for r in body.references if str(r).strip()]
    refs = list(dict.fromkeys(refs))
    if not refs:
        raise HTTPException(status_code=400, detail="No valid references provided.")

    if not _has_reconcile_evidence(body.provider_report):
        raise HTTPException(status_code=400, detail="provider_report evidence is required.")

    cursor = db["ramp_entries"].find({
        "direction": "off",
        "status": "completed",
        "$or": [
            {"providerReference": {"$in": refs}},
            {"_id": {"$in": refs}},
        ],
    })
    entries = await cursor.to_list(length=500)

    if not entries:
        return {
            "status": "ok",
            "message": "No completed withdrawals found for given references.",
            "requested": refs,
            "corrected": [],
            "skipped": refs,
            "failed": [],
        }

    corrected = []
    skipped = []
    failed = []

    for entry in entries:
        entry_id = str(entry.get("_id"))
        ref = str(entry.get("providerReference") or entry_id)

        # Idempotency guard: do not refund twice.
        if entry.get("correctionApplied"):
            skipped.append(ref)
            continue

        wallet_asset = entry.get("fromAsset") or "KES"
        amount = float(entry.get("fromAmount") or entry.get("toAmount") or 0)
        wallet_user_id = entry.get("userId")

        try:
            if body.refund_wallet and amount > 0:
                await db["retail_wallets"].update_one(
                    {"userId": wallet_user_id},
                    {"$inc": {wallet_asset: amount}},
                    upsert=True,
                )

            await db["ramp_entries"].update_one(
                {"_id": entry.get("_id")},
                {
                    "$set": {
                        "status": "failed",
                        "updatedAt": datetime.utcnow(),
                        "providerStatus": "MANUAL_CORRECTION",
                        "providerReport": body.provider_report,
                        "error_reason": "Manual correction: payout not received",
                        "correctionApplied": True,
                        "correctedBy": str(current_user.get("_id")),
                        "correctedAt": datetime.utcnow(),
                        "refundApplied": bool(body.refund_wallet),
                    }
                },
            )

            corrected.append({
                "reference": ref,
                "entryId": entry_id,
                "status": "failed",
                "refunded": bool(body.refund_wallet),
                "asset": wallet_asset,
                "amount": amount,
            })
        except Exception as exc:
            failed.append({"reference": ref, "reason": str(exc)})

    unresolved = [r for r in refs if r not in {c["reference"] for c in corrected} and r not in skipped]
    skipped.extend(unresolved)

    return {
        "status": "ok",
        "requested": refs,
        "corrected": corrected,
        "skipped": skipped,
        "failed": failed,
    }

    
#===================    

async def _process_airtel_c2b_payload(payload: dict, db):
    # 1. Safely parse and normalize the nested transaction block
    tx = payload.get("transaction", {}) if isinstance(payload.get("transaction"), dict) else {}

    # Extract reference using your existing helper
    reference = _extract_reference_from_payload(payload, tx)
    
    if not reference:
        print(f"⚠️ Airtel webhook ignored (missing reference). Payload: {payload}")
        return {"message": "Ignored: missing reference"}

    # 2. Native Airtel Carrier Status Parsing Layer
    # Airtel Africa OpenAPI uses explicit status_code: "TS" (Success) and "TF" (Failed)
    status_code = str(tx.get("status_code") or "").strip().upper()
    message = tx.get("message") or payload.get("status", {}).get("message", "")
    airtel_money_id = tx.get("airtel_money_id")

    if status_code == "TS":
        status = "SUCCESS"
        is_success = True
        failure_reason = None
    elif status_code == "TF":
        status = "FAILED"
        is_success = False
        failure_reason = message or "Transaction failed by carrier"
    else:
        # Fallback to secondary helper parsing if keys vary on alternate routes
        status, is_success, failure_reason = _extract_status_and_success(payload, tx)

    # 3. Database Lookup Array Verification
    # Search by provider reference (sent to gateway) first, fallback to internal _id
    entry = await db["ramp_entries"].find_one({"providerReference": str(reference)})
    if not entry:
        entry = await db["ramp_entries"].find_one({"_id": str(reference)})

    if not entry:
        print(f"⚠️ Airtel webhook reference not found: {reference}")
        return {"message": "Ignored: transaction not found"}

    # Diagnostic: log a concise summary of the DB entry we matched
    try:
        print("🔎 Matched ramp_entries:", {
            "_id": str(entry.get("_id")),
            "status": entry.get("status"),
            "userId": str(entry.get("userId")),
            "fromAsset": entry.get("fromAsset"),
            "fromAmount": entry.get("fromAmount"),
            "providerReference": entry.get("providerReference"),
        })
    except Exception:
        print("🔎 Matched ramp_entries (failed to stringify)")

    # 4. Normalize Status Checking to avoid Upper/Lower Case Guard blocks
    current_entry_status = str(entry.get("status") or "").lower()
    # If already completed, ignore duplicate callbacks.
    if current_entry_status == "completed":
        return {"message": f"Ignored: already {entry.get('status')}"}

    # Allow processing for entries that are processing/pending or previously failed
    # (supports late provider success callbacks). We'll enforce idempotency
    # by checking existing providerReport airtel id vs incoming airtel id.
    existing_report = entry.get("providerReport") if isinstance(entry.get("providerReport"), dict) else {}
    existing_airtel_id = existing_report.get("airtel_money_id") or existing_report.get("provider_tx_id")
    incoming_airtel_id = airtel_money_id
    if existing_airtel_id and incoming_airtel_id and str(existing_airtel_id) == str(incoming_airtel_id):
        return {"message": "Ignored: duplicate provider report"}

    # Extract internal metadata dimensions
    user_id = entry.get("userId")
    amount = float(entry.get("toAmount") or entry.get("fromAmount") or 0)
    wallet_asset = entry.get("fromAsset") or "KES"
    direction = str(entry.get("direction") or "on").lower()

    # 5. Core State Engine Actions
    if is_success:
        # Credit wallet only for deposit (on-ramp). Off-ramp funds were already deducted at request time.
        if direction == "on":
            try:
                wallet_update = await db["retail_wallets"].update_one(
                    {"userId": user_id},
                    {"$inc": {wallet_asset: amount}},
                    upsert=True,
                )
                print(f"➕ Wallet update result for user={user_id}: {getattr(wallet_update, 'raw_result', str(wallet_update))}")
            except Exception as e:
                print(f"❌ Error crediting wallet for user={user_id}: {e}")
                raise

        # Update ramp entries with active carrier values
        try:
            entry_update = await db["ramp_entries"].update_one(
                {"_id": entry["_id"]},
                {
                    "$set": {
                        "status": "completed",
                        "updatedAt": datetime.utcnow(),
                        "providerStatus": status,
                        "airtel_money_id": airtel_money_id,  # Track receipt metadata row
                        "providerReport": payload,
                        "processedByProvider": True,
                    }
                }
            )
            print(f"✔️ ramp_entries update result for _id={entry.get('_id')}: {getattr(entry_update, 'raw_result', str(entry_update))}")
        except Exception as e:
            print(f"❌ Error updating ramp_entries _id={entry.get('_id')}: {e}")
            raise

        if direction == "on":
            print(f"✅ Airtel STK success {reference}. Credited {amount} {wallet_asset} to {user_id}.")
        else:
            print(f"✅ Airtel withdrawal success {reference}. Marked completed for user {user_id}.")
    else:
        # Refund failed off-ramp requests because funds were optimistically deducted.
        if direction == "off":
            await db["retail_wallets"].update_one(
                {"userId": user_id},
                {"$inc": {wallet_asset: amount}},
                upsert=True
            )

        await db["ramp_entries"].update_one(
            {"_id": entry["_id"]},
            {
                "$set": {
                    "status": "failed",
                    "updatedAt": datetime.utcnow(),
                    "providerStatus": status,
                    "providerReport": payload,
                    "error_reason": failure_reason or "Deposit/payout failed or canceled",
                }
            }
        )
        if direction == "off":
            print(f"❌ Airtel withdrawal failed {reference}. Refunded {amount} {wallet_asset} to {user_id}.")
        else:
            print(f"❌ Airtel STK failed {reference}. Reason: {failure_reason}")

    return {"message": "C2B webhook processed"}


@router.post("/c2b/result")
async def airtel_c2b_webhook(request: Request, db=Depends(get_db)):
    return await _handle_airtel_callback(request, db, "legacy_c2b")


@callback_router.get("/health")
async def airtel_callback_health(request: Request):
    return {
        "status": "ok",
        "service": "airtel-callbacks",
        "path": str(request.url.path),
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


# Compatibility endpoints expected by the Go gateway.
# The gateway sends callbacks to /api/v1/callbacks/collections and
# /api/v1/callbacks/disbursements — forward these to the common handler.
@callback_router.post("/collections")
async def callback_collections(request: Request, db=Depends(get_db)):
    return await _handle_airtel_callback(request, db, "collections")


@callback_router.post("/disbursements")
async def callback_disbursements(request: Request, db=Depends(get_db)):
    return await _handle_airtel_callback(request, db, "disbursements")