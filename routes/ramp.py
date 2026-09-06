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
import math
import re
import httpx
from urllib.parse import parse_qs
from pymongo import ReturnDocument

from database import get_db
from services.safaricom_daraja import DarajaService
from routes.auth import get_current_user, get_current_user_with_role, get_verified_current_user, is_admin_role
from routes.treasury import get_or_create_rate_book, compute_swap_quote_from_book, DEFAULT_USD_BASE_RATES
from routes.swap_engine import settle_crypto_on_celo
from broadcast import broadcast_manager
from notifications import notify_user
from two_factor import verify_withdrawal_2fa
from wallet_utils import debit_wallet, credit_wallet
from cardano.airt import burn_airt, mint_airt, receipt_hash
from cardano.client import get_blockfrost_api
from cardano.wallet import CardanoWallet, get_or_create_wallet_index
from cardano.usda import send_usda
from services.impala_airtime import impala_airtime

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


def resolve_crypto_destination_address(candidate_address: str | None, user_doc: dict | None = None) -> str:
    """Resolve the real Celo destination for crypto settlement.

    Prefer the provided destination address; otherwise use the user's saved Celo wallet;
    if that is missing or is a non-Celo address (for example Cardano), fall back to the
    configured treasury Celo wallet so the swap can still settle on-chain without a manual entry.
    """
    def is_valid_celo_address(value: str | None) -> bool:
        return bool(value and re.fullmatch(r"0x[a-fA-F0-9]{40}", value.strip()))

    address = (candidate_address or "").strip()
    if address:
        if is_valid_celo_address(address):
            return address
        if user_doc and user_doc.get("walletAddress"):
            wallet_address = str(user_doc["walletAddress"]).strip()
            if is_valid_celo_address(wallet_address):
                return wallet_address
        fallback = os.getenv("CELO_HOT_WALLET_ADDRESS") or os.getenv("CELO_EXIT_ADDRESS")
        if is_valid_celo_address(fallback):
            return fallback
        raise ValueError(f"Invalid Celo wallet address: {address}")

    if user_doc and user_doc.get("walletAddress"):
        wallet_address = str(user_doc["walletAddress"]).strip()
        if is_valid_celo_address(wallet_address):
            return wallet_address

    fallback = os.getenv("CELO_HOT_WALLET_ADDRESS") or os.getenv("CELO_EXIT_ADDRESS")
    if is_valid_celo_address(fallback):
        return fallback

    raise ValueError("A valid 0x wallet address is required for crypto settlement.")


# Mobile-money provider detection + MSISDN validation shared between the
# deposit (on-ramp/STK) and withdrawal (off-ramp/B2C) handlers below — kept as
# one source of truth so the two branches can't drift apart the way they did
# before (off-ramp validation was still Airtel-only while its mobileMoneySP
# payload had already been made M-Pesa-aware).
#
# Safaricom ranges per the current Kenyan numbering plan: 070x/071x/072x/074x/
# 079x, 0110-0115, and 0757-0759/0768-0769. Airtel: 073x/075x/078x/010x.
_SAFARICOM_PREFIXES_2 = ("70", "71", "72", "74", "79")
_SAFARICOM_PREFIXES_3 = ("757", "758", "759", "768", "769")
_SAFARICOM_PREFIXES_110 = ("110", "111", "112", "113", "114", "115")
_AIRTEL_PREFIXES_2 = ("73", "75", "78", "10")


def resolve_momo_provider_and_validate(phone_local_9: str, requested_provider: str) -> tuple[bool, bool]:
    """Given a 9-digit local MSISDN (no leading 0/254) and the requested
    momo_provider, return (is_mpesa, is_valid_for_that_provider)."""
    requested = str(requested_provider or "Airtel").strip().lower()
    is_mpesa = requested in {"m-pesa", "mpesa", "safaricom"}

    if len(phone_local_9) != 9:
        return is_mpesa, False

    if is_mpesa:
        valid = (
            phone_local_9.startswith(_SAFARICOM_PREFIXES_2)
            or phone_local_9.startswith(_SAFARICOM_PREFIXES_3)
            or phone_local_9.startswith(_SAFARICOM_PREFIXES_110)
        )
    else:
        valid = phone_local_9.startswith(_AIRTEL_PREFIXES_2)

    return is_mpesa, valid

# Function for airtime reservetion 

async def _get_airtime_reserve_snapshot(db) -> dict[str, Any]:
    try:
        provider_result = await asyncio.to_thread(impala_airtime.get_payout_balance)
        provider_balance = float(provider_result["artm_balance"])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Unable to verify the provider airtime balance: {exc}")

    policy_id = os.getenv("CARDANO_AIRT_POLICY_ID", "").strip().lower()
    asset_name = os.getenv("CARDANO_AIRT_ASSET_NAME", "AIRT").encode("ascii").hex()
    if not policy_id:
        raise HTTPException(status_code=503, detail="CARDANO_AIRT_POLICY_ID is not configured.")
    try:
        asset = await asyncio.to_thread(get_blockfrost_api().asset, f"{policy_id}{asset_name}")
        circulating = float(getattr(asset, "quantity", 0) or 0)
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code == 404 or "404" in str(exc):
            circulating = 0.0
        else:
            raise HTTPException(status_code=503, detail=f"Unable to read Cardano AIRT supply: {exc}")
    return {
        "providerBalanceSnapshot": round(provider_balance, 6),
        "circulatingAirt": round(circulating, 6),
        "cardanoPolicyId": policy_id,
        "cardanoAssetName": os.getenv("CARDANO_AIRT_ASSET_NAME", "AIRT"),
        "asOf": datetime.utcnow(),
    }


async def _get_custody_airt_balance() -> float:
    custody_address = os.getenv("CARDANO_AIRT_CUSTODY_ADDRESS", "").strip() or os.getenv("MASTER_WALLET_ADDRESS", "").strip()
    policy_id = os.getenv("CARDANO_AIRT_POLICY_ID", "").strip().lower()
    asset_unit = f"{policy_id}{os.getenv('CARDANO_AIRT_ASSET_NAME', 'AIRT').encode('ascii').hex()}"
    if not custody_address or not policy_id:
        raise HTTPException(status_code=503, detail="AIRT custody address or policy is not configured.")
    try:
        address = await asyncio.to_thread(get_blockfrost_api().address, custody_address)
        for asset in getattr(address, "amount", []) or []:
            if str(getattr(asset, "unit", "")).lower() == asset_unit:
                return float(getattr(asset, "quantity", 0) or 0)
        return 0.0
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Unable to read Cardano AIRT custody balance: {exc}")


# function to record platform profit from swap transactions

async def _record_swap_profit(db, body, receive_amount, profit_amount, profit_currency, rate_book, trade_id):
    if profit_amount <= 0:
        return
    rates = dict(DEFAULT_USD_BASE_RATES)
    rates.update(rate_book.get("usd_base_rates", {}))
    profit_kes_equivalent = profit_amount * rates.get("KES", 130.50) / max(rates.get(profit_currency, 1.0), 0.00000001)
    await db["settlement_logs"].insert_one({
        "_id": f"PNL_{uuid.uuid4().hex[:6].upper()}",
        "desc": f"{body.amount} {body.from_asset} → {receive_amount} {body.to_asset} ({body.channel})",
        "profit_amount": round(profit_amount, 4),
        "profit_currency": profit_currency,
        "profit_kes_equivalent": round(profit_kes_equivalent, 2),
        "channel": body.channel,
        "timestamp": datetime.utcnow(),
        "status": "COMPLETED",
        "trade_id": trade_id,
    })
    await db["company_revenue"].update_one(
        {"_id": "corporate_treasury"},
        {"$inc": {profit_currency: profit_amount}},
        upsert=True,
    )

class RampExecute(BaseModel):
    direction: str
    channel: str
    from_asset: str
    to_asset: str
    amount: float
    rate: float
    fee: float
    counterparty: str
    provider_receipt_id: str = ""
    destination_address: str = ""
    momo_provider: str = ""
    # Only required/checked when direction == "off" (withdrawal) — see
    # two_factor.py. Deposits and swaps leave these blank.
    otp_session_id: str = ""
    otp_code: str = ""
    totp_code: str = ""


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
    support_case_id: str = ""
    reason: str = "Payout not received"


def _extract_reference_from_payload(payload: dict, tx: dict) -> str | None:
    """Extract reference id from varying Airtel/gateway payload shapes."""
    candidates = [
        tx.get("externalId"),
        tx.get("reference"),
        tx.get("id"),
        tx.get("clientReference"),
        tx.get("client_reference"),
        tx.get("merchantReference"),
        tx.get("transactionReference"),
        tx.get("gatewayReference"),
        tx.get("gateway_reference"),
        payload.get("externalId"),
        payload.get("reference"),
        payload.get("id"),
        payload.get("clientReference"),
        payload.get("client_reference"),
        payload.get("merchantReference"),
        payload.get("transactionReference"),
        payload.get("gatewayReference"),
        payload.get("gateway_reference"),
    ]

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    candidates.extend([
        data.get("externalId"),
        data.get("reference"),
        data.get("id"),
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
        or tx.get("transactionReport")
        or payload.get("message")
        or payload.get("transactionReport")
        or (status_obj.get("message") if status_obj else None)
        or None
    )

    success_words = ["SUCCESS", "SUCCESSFUL", "COMPLETE", "COMPLETED", "TS", "OK", "APPROVED", "PAID"]
    failure_words = ["FAILED", "FAIL", "ERROR", "REJECT", "DECLINED", "CANCEL", "CANCELLED", "WRONG PIN", "INVALID PIN", "TIMEOUT"]

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


async def _apply_wallet_delta_once(db, user_id, asset: str, amount: float, entry_id: str, marker_field: str) -> bool:
    """Apply a settlement credit/refund at most once for a ramp entry.

    The marker and balance update share one MongoDB document update, so a
    duplicate provider callback cannot duplicate customer value.
    """
    result = await db["retail_wallets"].update_one(
        {"userId": user_id, marker_field: {"$ne": entry_id}},
        {"$inc": {asset: amount}, "$addToSet": {marker_field: entry_id}},
    )
    if result.modified_count:
        return True
    wallet = await db["retail_wallets"].find_one({"userId": user_id}, {marker_field: 1})
    if not wallet:
        raise RuntimeError("Customer wallet is missing; settlement requires support reconciliation.")
    return False


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
async def execute_ramp(body: RampExecute, db=Depends(get_db), current_user=Depends(get_verified_current_user)):
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
        profit_currency = live_swap_quote.get("fee_currency", body.to_asset)

    # ========================================================
    # INTERNAL SWAP EXECUTION (User Ledger Transfer)
    # ========================================================
    if body.direction == "swap":
        receive_amount = float(receive)
        debit_amount = float(live_swap_quote.get("debit_amount", body.amount) if live_swap_quote else body.amount)
        
        # Lock & Verify Balance
        wallet = await db["retail_wallets"].find_one({"userId": {"$in": user_id_candidates}})
        current_balance = float(wallet.get(body.from_asset, 0)) if wallet else 0
        
        if current_balance < debit_amount:
            raise HTTPException(status_code=400, detail=f"Insufficient {body.from_asset} balance. You need {debit_amount:.4f}; you have {current_balance:.4f}.")

        user_doc = await db["users"].find_one({"_id": safe_object_id(user_id_raw)})
        destination_address = None
        if body.to_asset.upper() in {"CUSD", "USDC", "USDT", "USD"}:
            try:
                destination_address = resolve_crypto_destination_address(body.destination_address or user_doc.get("walletAddress") if user_doc else body.destination_address, user_doc)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        elif body.to_asset.upper() == "USDA":
            # Resolve Cardano destination address from user document or provided address
            if body.destination_address and body.destination_address.strip():
                destination_address = body.destination_address.strip()
            elif user_doc and user_doc.get("cardanoAddress"):
                destination_address = user_doc.get("cardanoAddress").strip()
            else:
                # Generate a new Cardano address for the user if not present
                try:
                    idx = await get_or_create_wallet_index(db, current_user.get("workspaceId", "demo_workspace"))
                    cardano_wallet = CardanoWallet(idx)
                    destination_address = cardano_wallet.address_str
                except Exception as exc:
                    raise HTTPException(status_code=400, detail=f"Unable to resolve Cardano address: {exc}") from exc

        if body.from_asset.upper() == "KES" and body.to_asset.upper() == "AIRT":
            wallet_owner_id = wallet.get("userId") if wallet else user_id
            receive_amount = math.floor(receive_amount)
            if receive_amount <= 0:
                raise HTTPException(status_code=400, detail="KES amount is too small to mint whole AIRT units.")
            reserve_snapshot = await _get_airtime_reserve_snapshot(db)
            available_before = max(
                reserve_snapshot["providerBalanceSnapshot"] - reserve_snapshot["circulatingAirt"],
                0,
            )
            reservation_id = f"AIRT-RES-{uuid.uuid4().hex[:10].upper()}"
            await db["airtime_mint_reservations"].update_one(
                {"_id": "AIRT-LIVE-RESERVE"},
                {"$setOnInsert": {"reservedAmount": 0.0, "createdAt": datetime.utcnow()}},
                upsert=True,
            )
            reservation = await db["airtime_mint_reservations"].find_one_and_update(
                {
                    "_id": "AIRT-LIVE-RESERVE",
                    "$expr": {"$lte": [{"$add": ["$reservedAmount", receive_amount]}, available_before]},
                },
                {
                    "$inc": {"reservedAmount": receive_amount},
                    "$set": {
                        "providerBalanceSnapshot": reserve_snapshot["providerBalanceSnapshot"],
                        "circulatingAirt": reserve_snapshot["circulatingAirt"],
                        "updatedAt": datetime.utcnow(),
                    },
                },
                return_document=ReturnDocument.AFTER,
            )
            if not reservation:
                raise HTTPException(status_code=409, detail="AIRT reserve is not currently available. Please try again later.")
            provider_name = os.getenv("AIRTIME_PROVIDER_NAME", "RESELLER")
            provider_receipt_id = f"LIVE-BALANCE-{reservation_id}"
            commitment = receipt_hash(provider_name, provider_receipt_id, receive_amount, "Kenya")
            try:
                await debit_wallet(db, user_id, "KES", debit_amount)
            except HTTPException:
                await db["airtime_mint_reservations"].update_one({"_id": "AIRT-LIVE-RESERVE"}, {"$inc": {"reservedAmount": -receive_amount}})
                raise
            operation_id = f"AIRT-SWAP-{uuid.uuid4().hex[:10].upper()}"
            await db["airtime_token_operations"].insert_one({
                "_id": operation_id, "type": "MINT", "status": "SUBMITTING", "userId": wallet_owner_id,
                "provider": provider_name, "country": "Kenya", "tokenAmount": receive_amount,
                "faceValueKes": receive_amount, "receiptId": provider_receipt_id,
                "receiptHash": commitment, "swapId": trade_id, "reservationId": reservation_id,
                "providerBalanceSnapshot": reserve_snapshot["providerBalanceSnapshot"],
                "circulatingAirtSnapshot": reserve_snapshot["circulatingAirt"],
                "createdAt": datetime.utcnow(),
            })
            try:
                funding_wallet = CardanoWallet(0)
                custody_address = os.getenv("CARDANO_AIRT_CUSTODY_ADDRESS", "").strip() or os.getenv("MASTER_WALLET_ADDRESS", "").strip() or funding_wallet.address_str
                mint_result = await asyncio.to_thread(
                    mint_airt, funding_wallet, custody_address, receive_amount,
                    provider_name, provider_receipt_id, "Kenya", operation_id,
                )
            except Exception as exc:
                await credit_wallet(db, user_id, "KES", debit_amount)
                await db["airtime_mint_reservations"].update_one({"_id": "AIRT-LIVE-RESERVE"}, {"$inc": {"reservedAmount": -receive_amount}})
                await db["airtime_token_operations"].update_one({"_id": operation_id}, {"$set": {"status": "FAILED", "error": str(exc), "updatedAt": datetime.utcnow()}})
                raise HTTPException(status_code=502, detail=f"Cardano AIRT mint failed; KES refunded: {exc}")
            await db["airtime_mint_reservations"].update_one({"_id": "AIRT-LIVE-RESERVE"}, {"$inc": {"reservedAmount": -receive_amount}})
            await db["airtime_token_operations"].update_one({"_id": operation_id}, {"$set": {"status": "CONFIRMED", "blockchainTxHash": mint_result["tx_hash"], "policyId": mint_result["policy_id"], "assetName": mint_result["asset_name"], "confirmedAt": datetime.utcnow()}})
            await credit_wallet(db, user_id, "AIRT", receive_amount)
            history_doc = {
                "_id": trade_id, "direction": "swap", "channel": "Cardano AIRT Mint",
                "fromAsset": "KES", "toAsset": "AIRT", "fromAmount": body.amount,
                "toAmount": receive_amount, "rate": live_swap_quote["execution_rate"],
                "marketRate": live_swap_quote["market_rate"], "fee": live_swap_quote["fee_amount"],
                "status": "completed", "userId": wallet_owner_id, "providerReceiptId": provider_receipt_id,
                "receiptHash": mint_result["receipt_hash"], "cardanoTxHash": mint_result["tx_hash"],
                "cardanoPolicyId": mint_result["policy_id"], "cardanoAssetName": mint_result["asset_name"],
                "createdAt": datetime.utcnow(), "date": datetime.utcnow().strftime("%b %d, %Y"), "timeAgo": "Just now",
            }
            await db["ramp_entries"].insert_one(history_doc)
            await db["airtime_history"].insert_one({
                "user_id": wallet_owner_id, "type": "Cardano AIRT Mint", "amount": receive_amount,
                "network": "Cardano", "country": "Kenya", "status": "Completed",
                "timestamp": datetime.utcnow(), "txHash": mint_result["tx_hash"], "receiptHash": mint_result["receipt_hash"],
            })
            await _record_swap_profit(db, body, receive_amount, profit_amount, profit_currency, rate_book, trade_id)
            return {"id": trade_id, "status": "completed", "receive": receive_amount, "cardanoTxHash": mint_result["tx_hash"], "cardanoPolicyId": mint_result["policy_id"], "cardanoAssetName": mint_result["asset_name"], "receiptHash": mint_result["receipt_hash"], "message": "KES exchanged for Cardano AIRT."}

        if body.from_asset.upper() == "AIRT" and body.to_asset.upper() == "KES":
            wallet_owner_id = wallet.get("userId") if wallet else user_id
            burn_amount = math.floor(body.amount) # Round down to whole AIRT units for burn
            if burn_amount <= 0:
                raise HTTPException(status_code=400, detail="AIRT redemption must be at least 1 whole token.")
            custody_balance = await _get_custody_airt_balance()
            if custody_balance < burn_amount:
                raise HTTPException(
                    status_code=409,
                    detail=f"Only {custody_balance:g} AIRT is available in Jasiri custody on Cardano. Reconcile the internal wallet before redeeming {burn_amount:g} AIRT.",
                )
            operation_id = f"AIRT-BURN-SWAP-{uuid.uuid4().hex[:10].upper()}"
            await debit_wallet(db, user_id, "AIRT", burn_amount)
            await db["airtime_token_operations"].insert_one({
                "_id": operation_id, "type": "BURN", "status": "SUBMITTING", "userId": wallet_owner_id,
                "tokenAmount": burn_amount, "faceValueKes": receive_amount, "swapId": trade_id,
                "createdAt": datetime.utcnow(),
            })
            try:
                burn_result = await asyncio.to_thread(burn_airt, CardanoWallet(0), burn_amount, operation_id)
            except Exception as exc:
                await credit_wallet(db, user_id, "AIRT", burn_amount)
                await db["airtime_token_operations"].update_one(
                    {"_id": operation_id},
                    {"$set": {"status": "FAILED", "error": str(exc), "updatedAt": datetime.utcnow()}},
                )
                raise HTTPException(status_code=502, detail=f"Cardano AIRT burn failed; AIRT refunded: {exc}")
            await credit_wallet(db, user_id, "KES", receive_amount)
            await db["airtime_token_operations"].update_one(
                {"_id": operation_id},
                {"$set": {"status": "CONFIRMED", "blockchainTxHash": burn_result["tx_hash"], "policyId": burn_result["policy_id"], "assetName": burn_result["asset_name"], "confirmedAt": datetime.utcnow()}},
            )
            await db["ramp_entries"].insert_one({
                "_id": trade_id, "direction": "swap", "channel": "Cardano AIRT Burn",
                "fromAsset": "AIRT", "toAsset": "KES", "fromAmount": burn_amount,
                "toAmount": receive_amount, "rate": live_swap_quote["execution_rate"],
                "marketRate": live_swap_quote["market_rate"], "fee": live_swap_quote["fee_amount"],
                "status": "completed", "userId": wallet_owner_id,
                "cardanoTxHash": burn_result["tx_hash"], "cardanoPolicyId": burn_result["policy_id"],
                "cardanoAssetName": burn_result["asset_name"], "operationId": operation_id,
                "createdAt": datetime.utcnow(), "date": datetime.utcnow().strftime("%b %d, %Y"), "timeAgo": "Just now",
            })
            await db["airtime_history"].insert_one({
                "_id": operation_id, "user_id": wallet_owner_id, "type": "Cardano AIRT Burn",
                "amount": burn_amount, "network": "Cardano", "status": "Completed",
                "timestamp": datetime.utcnow(), "txHash": burn_result["tx_hash"],
            })
            return {
                "id": trade_id, "status": "completed", "receive": receive_amount,
                "cardanoTxHash": burn_result["tx_hash"], "cardanoPolicyId": burn_result["policy_id"],
                "cardanoAssetName": burn_result["asset_name"], "message": "Cardano AIRT burned and KES credited.",
            }

        # Atomic Database Update
        wallet_owner_id = wallet.get("userId") if wallet else user_id
        before_settlement = body.from_asset
        settlement_result = None
        
        if body.to_asset.upper() in {"CUSD", "USDC", "USDT", "USD"}:
            # Debit the source asset BEFORE broadcasting anything on-chain — this
            # was previously missing entirely, so a KES/etc -> USDC swap sent real
            # crypto out of treasury AND credited the user's internal to_asset
            # balance without ever touching their from_asset balance: a repeatable
            # real-fund-drain bug, not a rounding edge case. Fixed 2026-09-04.
            await debit_wallet(db, user_id, body.from_asset, debit_amount)

            # "USD" is not its own on-chain token — there's no US banking rail
            # behind this platform, so a "USD" balance is a ledger label over a
            # real, fully-backed USDC settlement on Celo (the same rail already
            # used for USDC itself). The on-chain leg always moves real USDC;
            # only the internal ledger field is "USD". Added 2026-09-04 — see
            # the USD integration discussion in project memory.
            onchain_settlement_asset = "USDC" if body.to_asset.upper() == "USD" else body.to_asset
            settlement_result = await settle_crypto_on_celo(destination_address, onchain_settlement_asset, receive_amount)
            if not settlement_result.get("success"):
                await credit_wallet(db, user_id, body.from_asset, debit_amount)
                raise HTTPException(status_code=502, detail=f"On-chain settlement failed: {settlement_result.get('error', 'Unknown transfer error')}")
            await credit_wallet(db, user_id, body.to_asset, receive_amount)
            before_settlement = body.to_asset
        elif body.to_asset.upper() == "USDA":
            # Same missing-debit bug as the Celo branch above — fixed identically:
            # debit from_asset atomically before broadcasting, refund only if the
            # on-chain send actually fails.
            await debit_wallet(db, user_id, body.from_asset, debit_amount)

            # Real Cardano USDA settlement
            try:
                idx = await get_or_create_wallet_index(db, current_user.get("workspaceId", "demo_workspace"))
                cardano_wallet = CardanoWallet(idx)
                tx_hash = await asyncio.to_thread(send_usda, cardano_wallet, destination_address, receive_amount)
                settlement_result = {
                    "success": True,
                    "tx_hash": tx_hash,
                    "status": "completed",
                    "network": "cardano"
                }
            except Exception as exc:
                await credit_wallet(db, user_id, body.from_asset, debit_amount)
                raise HTTPException(status_code=502, detail=f"Cardano USDA settlement failed: {str(exc)}")

            # Only credit the user wallet after successful on-chain tx
            await credit_wallet(db, user_id, body.to_asset, receive_amount)
            before_settlement = body.to_asset
        else:
            # No $gte guard here previously — a plain combined $inc that could
            # push from_asset negative outright, on top of the same
            # single-row selection bug every other branch had.
            await debit_wallet(db, user_id, body.from_asset, body.amount)
            await credit_wallet(db, user_id, body.to_asset, receive_amount)

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
        
        # Add blockchain tx hash if settlement was on-chain
        if settlement_result and settlement_result.get("success"):
            if settlement_result.get("network") == "cardano":
                history_doc["cardanoTxHash"] = settlement_result.get("tx_hash")
            elif body.to_asset.upper() in {"CUSD", "USDC", "USDT", "USD"}:
                history_doc["celoTxHash"] = settlement_result.get("tx_hash")
        
        await db["ramp_entries"].insert_one(history_doc)

        await _record_swap_profit(db, body, receive_amount, profit_amount, profit_currency, rate_book, trade_id)

        await notify_user(
            db, user_id, "swap", "success",
            "Swap completed",
            f"{body.amount:g} {body.from_asset} → {receive_amount:g} {body.to_asset} completed instantly.",
            extra={"fromAsset": body.from_asset, "toAsset": body.to_asset, "entryId": trade_id},
        )

        return_data = {
            "id": trade_id,
            "status": "completed",
            "message": "Swap executed instantly.",
            "receive": receive_amount
        }
        
        # Include transaction hash if this was an on-chain settlement
        if settlement_result and settlement_result.get("success"):
            if settlement_result.get("network") == "cardano":
                return_data["cardanoTxHash"] = settlement_result.get("tx_hash")
            else:
                return_data["celoTxHash"] = settlement_result.get("tx_hash")
        
        return return_data

    # ========================================================
    # ON-RAMP (DEPOSIT KES VIA AIRTEL STK PUSH)
    # ========================================================
    if body.direction == "on" and body.channel == "Mobile Money":
        # 1. Sanitize Airtel MSISDN into the 9-digit local format accepted by Mamlaka.
        phone = str(body.counterparty).strip()
        phone = phone.replace(' ', '').replace('-', '')

        if phone.startswith('+'):
            phone = phone[1:]

        # Accept both 2547... and 07... and 7... forms and collapse them to provider-local 7XXXXXXXX.
        if phone.startswith('254'):
            phone = phone[3:]
        if phone.startswith('0'):
            phone = phone[1:]

        is_mpesa, valid_prefix = resolve_momo_provider_and_validate(phone, body.momo_provider)
        if not valid_prefix:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported MSISDN for {'M-Pesa' if is_mpesa else 'Airtel STK'}. Use a valid Kenyan mobile-money number.",
            )

        # 2. Call the configured Mamlaka sandbox gateway. Airtel can post the
        # result directly to this application's public callback URL.
        gateway_url = os.environ.get("AIRTEL_API_BASE_URL", "").rstrip("/")
        api_username = os.environ.get("AIRTEL_API_USERNAME", "")
        api_password = os.environ.get("AIRTEL_API_PASSWORD", "")
        if not gateway_url or not api_username or not api_password:
            raise HTTPException(status_code=503, detail="Airtel sandbox API credentials are not configured.")
        gateway_body: dict[str, Any] = {}
        gateway_ack_id: str | None = None
        gateway_status_message: str | None = None
        gateway_http_status: int | None = None
        gateway_response_text: str | None = None
        e164_phone = f"254{phone}"
        national_phone = f"0{phone}"

        try:
            callback_base_url = _resolve_airtel_callback_base_url()

            payload = {
                "impalaMerchantId": api_username,
                "payerPhone": f"254{phone}",
                "amount": int(body.amount),
                "currency": "KES",
                "mobileMoneySP": "Safaricom" if is_mpesa else "airtel",
                "externalId": provider_reference,
            }
            collection_callback = (
                f"{callback_base_url}/api/v1/callbacks/collections"
                if callback_base_url else None
            )
            # Keep the old external relay available only as an explicit fallback.
            use_portal_callback = os.environ.get("AIRTEL_USE_PORTAL_CALLBACK", "false").strip().lower() == "true"
            portal_callback = (
                os.environ.get("AIRTEL_PORTAL_COLLECTIONS_CALLBACK_URL", "").strip()
                if use_portal_callback else ""
            )
            if collection_callback or portal_callback:
                payload["callbackUrl"] = portal_callback or collection_callback

            # 3. Authenticate to Mamlaka, then request the Airtel STK push.
            async with httpx.AsyncClient() as client:
                auth_response = await client.get(f"{gateway_url}/", auth=(api_username, api_password), timeout=15.0)
                if auth_response.status_code == 401:
                    raise HTTPException(
                        status_code=502,
                        detail="Airtel sandbox authentication failed (401 Unauthorized). Verify AIRTEL_API_USERNAME and AIRTEL_API_PASSWORD with the gateway operator.",
                    )
                auth_response.raise_for_status()
                auth_body = auth_response.json()
                access_token = auth_body.get("token") if isinstance(auth_body, dict) else None
                if not access_token:
                    raise HTTPException(status_code=502, detail="Airtel sandbox authentication response did not include a token.")
                headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
                stk_url = f"{gateway_url}/mobile/initiate"
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
                        gateway_body.get("transactionId")
                        or gateway_body.get("transaction_id")
                        or gateway_body.get("reference")
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
            "statusHistory": [{"state": "processing", "at": datetime.utcnow(), "source": "provider_request_accepted"}],
            "userId": user_id,
            "phone": phone,
            "phoneE164": e164_phone,
            "phoneNational": national_phone,
            "providerReference": provider_reference,
            "mobileMoneyProvider": "Safaricom" if is_mpesa else "Airtel",
            "reference": provider_reference,
            "callbackUrl": collection_callback,
            "providerCallbackUrl": portal_callback or collection_callback,
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
            "statusHistory": [{"state": "processing", "at": datetime.utcnow(), "source": "provider_request_accepted"}],
            "message": f"{'M-Pesa' if is_mpesa else 'Airtel'} STK request sent to provider.",
            "provider": "Safaricom" if is_mpesa else "Airtel",
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
        # Verified before anything else touches the balance — a failed 2FA
        # check must never have already moved funds.
        await verify_withdrawal_2fa(db, current_user, body.otp_session_id, body.otp_code, body.totp_code)

        # Sums/debits across every retail_wallets row for this user — see
        # wallet_utils.py. The previous single-row find_one_and_update could
        # reject a withdrawal as "insufficient" even when the summed balance
        # (what GET /wallet actually displays) covered it, whenever funds were
        # split across a legacy string-keyed row and a newer ObjectId-keyed one.
        await debit_wallet(db, user_id, "KES", body.amount)
        wallet_owner_id = user_id
        alphanumeric_ref = f"B2C{uuid.uuid4().hex[:20].upper()}"
        requested_provider = str(body.momo_provider or "Airtel").strip().lower()
        is_mpesa = requested_provider in {"m-pesa", "mpesa", "safaricom"}

        # Persist the pending row before the provider call. Mamlaka can send
        # the callback before its request response reaches this handler.
        await db["ramp_entries"].insert_one({
            "_id": trade_id,
            "direction": body.direction,
            "channel": body.channel,
            "fromAsset": body.from_asset,
            "toAsset": body.to_asset,
            "fromAmount": body.amount,
            "toAmount": receive,
            "status": "processing",
            "statusHistory": [{"state": "processing", "at": datetime.utcnow(), "source": "provider_request_started"}],
            "userId": wallet_owner_id,
            "providerReference": alphanumeric_ref,
            "mobileMoneyProvider": "Safaricom" if is_mpesa else "Airtel",
            "date": datetime.utcnow().strftime("%b %d, %Y"),
            "timeAgo": "Just now",
            "createdAt": datetime.utcnow(),
        })

        try:
            # --- MAMLAKA SANDBOX GATEWAY INTEGRATION ---
            gateway_url = os.environ.get("AIRTEL_API_BASE_URL", "").rstrip("/")
            api_username = os.environ.get("AIRTEL_API_USERNAME", "")
            api_password = os.environ.get("AIRTEL_API_PASSWORD", "")
            if not gateway_url or not api_username or not api_password:
                raise HTTPException(status_code=503, detail="Airtel sandbox API credentials are not configured.")
            payout_url = f"{gateway_url}/mobile/transfer"

            # Normalize Airtel phone to the provider contract: 9 digits local form
            phone_for_airtel = str(body.counterparty).strip().replace(' ', '').replace('-', '')
            if phone_for_airtel.startswith('+'):
                phone_for_airtel = phone_for_airtel[1:]
            if phone_for_airtel.startswith('254'):
                phone_for_airtel = phone_for_airtel[3:]
            if phone_for_airtel.startswith('0'):
                phone_for_airtel = phone_for_airtel[1:]

            _, valid_prefix = resolve_momo_provider_and_validate(phone_for_airtel, requested_provider)
            if not valid_prefix:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Unsupported MSISDN for M-Pesa payout. Use a Safaricom number (07XXXXXXXX / 2547XXXXXXXX)."
                        if is_mpesa else
                        "Unsupported MSISDN for Airtel payout. Use an Airtel Money number (07XXXXXXXX / 2547XXXXXXXX)."
                    ),
                )

            payload = {
                "impalaMerchantId": api_username,
                "recipientPhone": f"254{phone_for_airtel}",
                "amount": int(receive),
                "currency": "KES",
                "mobileMoneySP": "M-Pesa" if is_mpesa else "Airtel",
                "externalId": alphanumeric_ref,
            }

            callback_base_url = _resolve_airtel_callback_base_url()
            disburse_callback = (
                f"{callback_base_url}/api/v1/callbacks/disbursements"
                if callback_base_url else None
            )
            if disburse_callback:
                payload["callback_url"] = disburse_callback
                payload["callbackUrl"] = disburse_callback

            async with httpx.AsyncClient() as client:
                auth_response = await client.get(f"{gateway_url}/", auth=(api_username, api_password), timeout=15.0)
                auth_response.raise_for_status()
                auth_body = auth_response.json()
                access_token = auth_body.get("token") if isinstance(auth_body, dict) else None
                if not access_token:
                    raise HTTPException(status_code=502, detail="Airtel sandbox authentication response did not include a token.")
                headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
                response = await client.post(payout_url, json=payload, headers=headers, timeout=15.0)
                if response.is_error:
                    response_text = response.text
                    print(f"Airtel Gateway Rejected: {response_text}")
                    await credit_wallet(db, wallet_owner_id, "KES", body.amount)
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
                        await credit_wallet(db, wallet_owner_id, "KES", body.amount)
                        raise HTTPException(status_code=400, detail=f"Airtel gateway business rejection: {response_text}")

        except HTTPException:
            raise
        except Exception as e:
            print(f"Gateway Connection Error: {e}")
            await credit_wallet(db, wallet_owner_id, "KES", body.amount)
            raise HTTPException(status_code=502, detail="Failed to connect to Airtel Gateway. Funds refunded.")

        # The callback may already have completed this row. Only enrich it
        # with provider request metadata and preserve its current status.
        await db["ramp_entries"].update_one(
            {"_id": trade_id},
            {"$set": {"providerRequestAcceptedAt": datetime.utcnow(), "updatedAt": datetime.utcnow()}},
        )

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
            "statusHistory": [
                {
                    "state": item.get("state"),
                    "at": item.get("at").isoformat() + "Z" if isinstance(item.get("at"), datetime) else item.get("at"),
                    "source": item.get("source"),
                }
                for item in e.get("statusHistory", [])
                if isinstance(item, dict)
            ],
            "providerReference": e.get("providerReference"),
            "mobileMoneyProvider": e.get("mobileMoneyProvider"),
            "errorReason": e.get("error_reason"),
            "error": e.get("error"),
            "providerStatus": e.get("providerStatus"),
            "supportCaseId": e.get("supportCaseId"),
            "date": e.get("date", "Today"), 
            "timeAgo": e.get("timeAgo", "Recently"),
            # Send proper ISO timestamp to the frontend
            "createdAt": e.get("createdAt", datetime.utcnow()).isoformat() + "Z" if e.get("createdAt") else None
            ,"cardanoTxHash": e.get("cardanoTxHash")
            ,"cardanoPolicyId": e.get("cardanoPolicyId")
            ,"cardanoAssetName": e.get("cardanoAssetName")
            ,"receiptHash": e.get("receiptHash")
            ,"explorerUrl": f"https://cardanoscan.io/transaction/{e['cardanoTxHash']}" if e.get("cardanoTxHash") else None
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
        "status": {"$in": ["processing", "pending", "provider_confirmed"]},
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
            {"_id": entry.get("_id"), "status": {"$in": ["processing", "pending", "provider_confirmed"]}},
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
                await _apply_wallet_delta_once(
                    db, wallet_user_id, wallet_asset, amount, entry_id, "appliedRampCredits"
                )

            await db["ramp_entries"].update_one(
                {"_id": entry.get("_id")},
                {
                    "$set": {
                        "status": "credited" if desired == "completed" else "failed",
                        "updatedAt": datetime.utcnow(),
                        "providerStatus": "MANUAL_RECONCILE",
                        "providerReport": body.provider_report or {"source": "manual_reconcile"},
                        "reconciledBy": str(current_user.get("_id")),
                        "error_reason": "Manual reconcile marked failed" if desired == "failed" else None,
                    },
                    "$push": {"statusHistory": {"state": "credited" if desired == "completed" else "failed", "at": datetime.utcnow(), "source": "manual_reconcile"}},
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
        "status": {"$in": ["processing", "pending", "provider_confirmed"]},
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
            {"_id": entry.get("_id"), "status": {"$in": ["processing", "pending", "provider_confirmed"]}},
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
                await _apply_wallet_delta_once(
                    db, wallet_user_id, wallet_asset, amount, entry_id, "appliedRampRefunds"
                )

            await db["ramp_entries"].update_one(
                {"_id": entry.get("_id")},
                {
                    "$set": {
                        "status": "credited" if desired == "completed" else "failed",
                        "updatedAt": datetime.utcnow(),
                        "providerStatus": "MANUAL_RECONCILE",
                        "providerReport": body.provider_report or {"source": "manual_withdraw_reconcile"},
                        "reconciledBy": str(current_user.get("_id")),
                        "error_reason": "Manual reconcile marked failed" if desired == "failed" else None,
                    },
                    "$push": {"statusHistory": {"state": "credited" if desired == "completed" else "failed", "at": datetime.utcnow(), "source": "manual_reconcile"}},
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
        "status": {"$in": ["completed", "credited"]},
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
                await _apply_wallet_delta_once(
                    db, wallet_user_id, wallet_asset, amount, entry_id, "appliedSupportReversals"
                )

            await db["ramp_entries"].update_one(
                {"_id": entry.get("_id")},
                {
                    "$set": {
                        "status": "reversed",
                        "updatedAt": datetime.utcnow(),
                        "providerStatus": "MANUAL_CORRECTION",
                        "providerReport": body.provider_report,
                        "error_reason": body.reason.strip() or "Manual correction: payout not received",
                        "correctionApplied": True,
                        "supportCaseId": body.support_case_id.strip() or None,
                        "correctedBy": str(current_user.get("_id")),
                        "correctedAt": datetime.utcnow(),
                        "refundApplied": bool(body.refund_wallet),
                    },
                    "$push": {"statusHistory": {"state": "reversed", "at": datetime.utcnow(), "source": "support_reversal", "caseId": body.support_case_id.strip() or None}},
                },
            )

            corrected.append({
                "reference": ref,
                "entryId": entry_id,
                "status": "reversed",
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
    status_payload = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    message = tx.get("message") or status_payload.get("message", "")
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
    # The provider may callback immediately, before execute_ramp has inserted
    # the accepted request. Retry briefly so that callback is not lost.
    entry = None
    for attempt in range(5):
        entry = await db["ramp_entries"].find_one({"providerReference": str(reference)})
        if not entry:
            entry = await db["ramp_entries"].find_one({"_id": str(reference)})
        if entry:
            break
        if attempt < 4:
            await asyncio.sleep(0.25)

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

    # 4. State and duplicate guards. A callback may be delivered more than once;
    # terminal or support-owned entries must never be processed automatically.
    current_entry_status = str(entry.get("status") or "").lower()
    if current_entry_status in {"credited", "completed", "failed", "reversed"}:
        return {"message": f"Ignored: already {entry.get('status')}"}
    if current_entry_status not in {"pending", "processing"}:
        return {"message": f"Ignored: {entry.get('status')} requires reconciliation"}

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
        # Claim this entry before side effects. Only one concurrent callback can
        # move it out of pending/processing, which makes provider retries safe.
        claim = await db["ramp_entries"].update_one(
            {"_id": entry["_id"], "status": {"$in": ["pending", "processing"]}},
            {
                "$set": {
                    "status": "provider_confirmed",
                    "providerConfirmedAt": datetime.utcnow(),
                    "updatedAt": datetime.utcnow(),
                    "providerStatus": status,
                    "airtel_money_id": airtel_money_id,
                    "externalId": payload.get("externalId"),
                    "secureId": payload.get("secureId"),
                    "providerReport": payload,
                    "processedByProvider": True,
                },
                "$push": {"statusHistory": {"state": "provider_confirmed", "at": datetime.utcnow(), "source": "airtel_callback"}},
            },
        )
        if claim.modified_count == 0:
            return {"message": "Ignored: callback was already claimed"}

        # Credit wallet only for a deposit. Off-ramp funds were locked when the
        # provider request was accepted. The marker prevents duplicate credits.
        if direction == "on":
            try:
                await _apply_wallet_delta_once(
                    db, user_id, wallet_asset, amount, str(entry.get("_id")), "appliedRampCredits"
                )
                try:
                    await broadcast_manager.send_user(str(user_id), {
                        "type": "wallet_update",
                        "userId": str(user_id),
                        "asset": wallet_asset,
                        "amount": amount,
                        "entryId": str(entry.get("_id")),
                    })
                except Exception:
                    pass
            except Exception as e:
                print(f"❌ Error crediting wallet for user={user_id}: {e}")
                raise

        # Complete only after the value movement is durably recorded.
        try:
            entry_update = await db["ramp_entries"].update_one(
                {"_id": entry["_id"], "status": "provider_confirmed"},
                {
                    "$set": {
                        "status": "completed",
                        "completedAt": datetime.utcnow(),
                        "updatedAt": datetime.utcnow(),
                    },
                    "$push": {"statusHistory": {"state": "completed", "at": datetime.utcnow(), "source": "wallet_settlement"}},
                }
            )
            print(f"✔️ ramp_entries update result for _id={entry.get('_id')}: {getattr(entry_update, 'raw_result', str(entry_update))}")
        except Exception as e:
            print(f"❌ Error updating ramp_entries _id={entry.get('_id')}: {e}")
            raise

        if direction == "on":
            print(f"✅ Airtel STK success {reference}. Credited {amount} {wallet_asset} to {user_id}.")
            try:
                await broadcast_manager.send_user(str(user_id), {
                    "type": "stk_success",
                    "userId": str(user_id),
                    "asset": wallet_asset,
                    "amount": amount,
                    "entryId": str(entry.get("_id")),
                })
            except Exception:
                pass
            await notify_user(
                db, user_id, "deposit", "success",
                "Deposit received",
                f"Your account was credited {amount:g} {wallet_asset}.",
                extra={"asset": wallet_asset, "amount": amount, "entryId": str(entry.get("_id"))},
            )
        else:
            print(f"✅ Airtel withdrawal success {reference}. Settlement credited for user {user_id}.")
            try:
                await broadcast_manager.send_user(str(user_id), {
                    "type": "withdrawal_success",
                    "userId": str(user_id),
                    "asset": wallet_asset,
                    "amount": amount,
                    "entryId": str(entry.get("_id")),
                })
            except Exception:
                pass
            await notify_user(
                db, user_id, "withdrawal", "success",
                "Withdrawal completed",
                f"Your withdrawal of {amount:g} {wallet_asset} was completed.",
                extra={"asset": wallet_asset, "amount": amount, "entryId": str(entry.get("_id"))},
            )
    else:
        # Claim failure before refunding so a repeated failure callback cannot
        # create a second customer credit.
        claim = await db["ramp_entries"].update_one(
            {"_id": entry["_id"], "status": {"$in": ["pending", "processing"]}},
            {
                "$set": {
                    "status": "reversing",
                    "updatedAt": datetime.utcnow(),
                    "providerStatus": status,
                    "externalId": payload.get("externalId"),
                    "secureId": payload.get("secureId"),
                    "providerReport": payload,
                },
                "$push": {"statusHistory": {"state": "reversing", "at": datetime.utcnow(), "source": "airtel_callback"}},
            },
        )
        if claim.modified_count == 0:
            return {"message": "Ignored: failure callback was already claimed"}

        # Refund failed off-ramp requests because funds were optimistically deducted.
        if direction == "off":
            await _apply_wallet_delta_once(
                db, user_id, wallet_asset, amount, str(entry.get("_id")), "appliedRampRefunds"
            )
            try:
                await broadcast_manager.send_user(str(user_id), {
                    "type": "withdrawal_failed_refund",
                    "userId": str(user_id),
                    "asset": wallet_asset,
                    "amount": amount,
                    "entryId": str(entry.get("_id")),
                    "reason": failure_reason,
                })
            except Exception:
                pass

        await db["ramp_entries"].update_one(
            {"_id": entry["_id"], "status": "reversing"},
            {
                "$set": {
                    "status": "failed",
                    "updatedAt": datetime.utcnow(),
                    "providerStatus": status,
                    "externalId": payload.get("externalId"),
                    "secureId": payload.get("secureId"),
                    "providerReport": payload,
                    "error_reason": failure_reason or "Deposit/payout failed or canceled",
                },
                "$push": {"statusHistory": {"state": "failed", "at": datetime.utcnow(), "source": "provider_failure"}},
            }
        )
        if direction == "off":
            print(f"❌ Airtel withdrawal failed {reference}. Refunded {amount} {wallet_asset} to {user_id}.")
            await notify_user(
                db, user_id, "withdrawal", "error",
                "Withdrawal failed",
                f"Your withdrawal of {amount:g} {wallet_asset} failed and was refunded. {failure_reason or ''}".strip(),
                extra={"asset": wallet_asset, "amount": amount, "entryId": str(entry.get("_id"))},
            )
        else:
            provider_name = entry.get("mobileMoneyProvider") or "mobile-money provider"
            print(f"❌ {provider_name} STK failed {reference}. Reason: {failure_reason}")
            await notify_user(
                db, user_id, "deposit", "error",
                "Deposit failed",
                f"Your deposit of {amount:g} {wallet_asset} could not be completed. {failure_reason or ''}".strip(),
                extra={"asset": wallet_asset, "amount": amount, "entryId": str(entry.get("_id"))},
            )

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
