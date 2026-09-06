import uuid
import os
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import asyncio

from database import get_db
from routes.auth import get_current_user, get_current_user_with_role, get_verified_current_user, is_admin_role
from services.safaricom_daraja import DarajaService

from cardano.airt import mint_airt, receipt_hash
from cardano.wallet import CardanoWallet
from services.impala_airtime import impala_airtime
from notifications import notify_user

router = APIRouter(prefix="/api/airtime", tags=["Airtime Tokenization"])
mam_laka = DarajaService()

# --- Pydantic Schemas ---
class MintRequest(BaseModel):
    amount: float
    network: str
    country: str
    note: str = ""
    provider_receipt_id: str = ""
    destination_address: str = ""

class RedeemRequest(BaseModel):
    amount: float
    phone: str
    provider: str = "AIRTEL"

class ReserveAttestation(BaseModel):
    provider: str
    receipt_id: str
    amount_kes: float
    country: str = "Kenya"

def _serialize_datetime(value):
    return value.isoformat() + "Z" if isinstance(value, datetime) else value

async def _provider_reserve():
    try:
        result = await asyncio.to_thread(impala_airtime.get_payout_balance)
        return float(result["artm_balance"])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Unable to verify telecom provider reserve: {exc}")

async def _reserve_snapshot(db):
    provider_reserve = await _provider_reserve()
    minted = await db["airtime_token_operations"].aggregate([
        {"$match": {"type": "MINT", "status": "CONFIRMED"}},
        {"$group": {"_id": None, "amount": {"$sum": "$tokenAmount"}}},
    ]).to_list(1)
    burned = await db["airtime_token_operations"].aggregate([
        {"$match": {"type": "BURN", "status": "CONFIRMED"}},
        {"$group": {"_id": None, "amount": {"$sum": "$tokenAmount"}}},
    ]).to_list(1)
    minted_amount = float(minted[0]["amount"]) if minted else 0.0
    burned_amount = float(burned[0]["amount"]) if burned else 0.0
    circulating = max(minted_amount - burned_amount, 0.0)
    return {
        "providerReserveKes": round(provider_reserve, 2),
        "mintedAirt": round(minted_amount, 6),
        "burnedAirt": round(burned_amount, 6),
        "circulatingAirt": round(circulating, 6),
        "unbackedAirt": round(max(circulating - provider_reserve, 0), 6),
        "reserveRatio": round(provider_reserve / circulating, 6) if circulating else None,
        "cardanoPolicyConfigured": bool(os.getenv("CARDANO_AIRT_POLICY_ID") and os.getenv("CARDANO_AIRT_POLICY_SIGNING_KEY")),
        "asOf": datetime.utcnow().isoformat() + "Z",
    }

# --- Endpoints ---
@router.get("/summary")
async def get_summary(db=Depends(get_db)):
    """Fetches real live balances DIRECTLY from Mam-laka API."""
    live_balance_res = mam_laka.get_merchant_balance()
    
    live_artm = 0.0
    if live_balance_res.get("status") == "success":
        live_artm = live_balance_res["data"].get("artmBalance", 0.0)
    
    reserve = await _reserve_snapshot(db)
    return {
        "live_artm_balance": live_artm,
        "internal_airt": live_artm,
        "internal_imp": live_artm,
        "reserve": reserve,
    }

@router.get("/tokenization/overview")
async def get_tokenization_overview(db=Depends(get_db), current_user=Depends(get_current_user)):
    """Return the auditable reserve and Cardano readiness state for AIRT."""
    return {"status": "success", "reserve": await _reserve_snapshot(db)}

@router.get("/tokenization/operations")
async def get_tokenization_operations(limit: int = 50, db=Depends(get_db), current_user=Depends(get_current_user)):
    operations = await db["airtime_token_operations"].find({"userId": current_user.get("_id")}).sort("createdAt", -1).limit(min(limit, 100)).to_list(min(limit, 100))
    for operation in operations:
        operation["id"] = str(operation.pop("_id"))
        operation["createdAt"] = _serialize_datetime(operation.get("createdAt"))
    return {"status": "success", "operations": operations}

@router.post("/reserve/attest")
async def attest_reserve(body: ReserveAttestation, db=Depends(get_db), current_user=Depends(get_current_user_with_role)):
    """Register a provider-confirmed reserve lot once; customers never handle this evidence."""
    if not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Admin role required")
    if body.amount_kes <= 0 or not body.receipt_id.strip():
        raise HTTPException(status_code=400, detail="A valid receipt and positive reserve amount are required.")
    existing = await db["airtime_reserve_lots"].find_one({"provider": body.provider.upper(), "receiptId": body.receipt_id.strip()})
    if existing:
        raise HTTPException(status_code=409, detail="This provider receipt is already registered.")
    lot_id = f"AIRT-RESERVE-{uuid.uuid4().hex[:10].upper()}"
    await db["airtime_reserve_lots"].insert_one({
        "_id": lot_id, "provider": body.provider.upper(), "receiptId": body.receipt_id.strip(),
        "amountKes": float(body.amount_kes), "reservedAmountKes": 0.0, "country": body.country,
        "status": "CONFIRMED", "createdAt": datetime.utcnow(), "createdBy": current_user.get("_id"),
    })
    return {"status": "success", "lotId": lot_id, "message": "Provider reserve registered for automatic customer tokenization."}

@router.get("/history")
async def get_history(db=Depends(get_db), current_user=Depends(get_current_user)):
    """Fetches airtime tokenization history ONLY for the logged-in user."""
    user_id = current_user.get("_id")
    
    cursor = db["airtime_history"].find({"user_id": user_id}).sort("timestamp", -1).limit(50)
    history = await cursor.to_list(length=50)
    
    formatted = []
    for h in history:
        formatted.append({
            "id": str(h["_id"]),
            "type": h.get("type", "Redemption"),
            "amount": h.get("amount", 0.0),
            "usd": h.get("usd", 0.0),
            "network": h.get("network", "Unknown"),
            "country": h.get("country", "Kenya"),
            "status": h.get("status", "Completed"),
            "error": h.get("error"),
            "time": h.get("timestamp").strftime("%b %d, %H:%M") if h.get("timestamp") else "Just now"
        })
    return {"status": "success", "history": formatted}

@router.post("/mint")
async def mint_imp(req: MintRequest, db=Depends(get_db), current_user=Depends(get_verified_current_user)):
    """Mint AIRT only after a provider receipt and Cardano policy are verified."""
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Mint amount must be greater than zero.")
    if not req.provider_receipt_id.strip():
        raise HTTPException(status_code=400, detail="Provider receipt ID is required.")
    if not req.destination_address.strip():
        raise HTTPException(status_code=400, detail="Cardano destination address is required.")
    reserve = await _reserve_snapshot(db)
    if not reserve["cardanoPolicyConfigured"]:
        raise HTTPException(status_code=503, detail="AIRT Cardano policy credentials are not configured.")
    if req.amount > reserve["providerReserveKes"] - reserve["circulatingAirt"]:
        raise HTTPException(status_code=409, detail="Insufficient verified airtime reserve to back this mint.")

    operation_id = f"AIRT-MINT-{uuid.uuid4().hex[:10].upper()}"
    user_id = current_user.get("_id")
    commitment = receipt_hash(req.network, req.provider_receipt_id, req.amount, req.country)
    if await db["airtime_token_operations"].find_one({"receiptHash": commitment}):
        raise HTTPException(status_code=409, detail="This provider receipt has already been used.")
    await db["airtime_token_operations"].insert_one({
        "_id": operation_id, "type": "MINT", "status": "SUBMITTING", "userId": user_id,
        "provider": req.network.upper(), "country": req.country, "tokenAmount": float(req.amount),
        "faceValueKes": float(req.amount), "receiptId": req.provider_receipt_id.strip(),
        "receiptHash": commitment, "blockchainTxHash": None, "createdAt": datetime.utcnow(),
    })
    try:
        result = await asyncio.to_thread(
            mint_airt, CardanoWallet(0), req.destination_address.strip(), req.amount,
            req.network, req.provider_receipt_id, req.country, operation_id,
        )
    except Exception as e:
        await db["airtime_token_operations"].update_one({"_id": operation_id}, {"$set": {"status": "FAILED", "error": str(e), "updatedAt": datetime.utcnow()}})
        raise HTTPException(status_code=502, detail=f"Cardano AIRT mint failed: {e}")
    await db["airtime_token_operations"].update_one({"_id": operation_id}, {"$set": {"status": "CONFIRMED", "blockchainTxHash": result["tx_hash"], "policyId": result["policy_id"], "assetName": result["asset_name"], "confirmedAt": datetime.utcnow()}})
    
    # Log to History for UI
    await db["airtime_history"].insert_one({
        "user_id": user_id,
        "type": "Mint Token",
        "amount": result["token_amount"],
        "usd": req.amount / 130.5,
        "network": req.network.upper(),
        "country": req.country,
        "status": "Completed",
        "timestamp": datetime.utcnow(),
        "txHash": result["tx_hash"]
    })

    return {
        "status": "success", 
        "operationId": operation_id, 
        "txHash": result["tx_hash"],
        "receiptHash": result["receipt_hash"],
        "message": f"Successfully minted {req.amount} AIRT on Cardano!"
    }

@router.post("/redeem")
async def redeem_airtime(req: RedeemRequest, db=Depends(get_db), current_user=Depends(get_verified_current_user)):
    if req.amount <= 0 or req.amount != int(req.amount):
        raise HTTPException(status_code=400, detail="AIRT redemption must be a positive whole number.")
    if not req.phone.strip():
        raise HTTPException(status_code=400, detail="A destination phone number is required.")

    user_id = current_user.get("_id")
    operation_id = f"AIRT-REDEEM-{uuid.uuid4().hex[:10].upper()}"
    wallet = await db["retail_wallets"].find_one({"userId": user_id})
    available = float(wallet.get("AIRT", 0) or 0) if wallet else 0.0
    if available < req.amount:
        raise HTTPException(status_code=400, detail=f"Insufficient AIRT balance. You only have {available:g} AIRT.")

    debit = await db["retail_wallets"].update_one(
        {"userId": user_id, "AIRT": {"$gte": req.amount}},
        {"$inc": {"AIRT": -req.amount}},
    )
    if debit.modified_count == 0:
        raise HTTPException(status_code=409, detail="AIRT balance changed. Please refresh and try again.")

    await db["airtime_token_operations"].insert_one({
        "_id": operation_id,
        "type": "REDEEM",
        "status": "SUBMITTING",
        "userId": user_id,
        "tokenAmount": int(req.amount),
        "faceValueKes": int(req.amount),
        "phone": req.phone.strip(),
        "provider": req.provider.upper(),
        "createdAt": datetime.utcnow(),
    })

    try:
        result = await asyncio.to_thread(impala_airtime.send_airtime, req.phone.strip(), int(req.amount), operation_id)
    except Exception as exc:
        await db["retail_wallets"].update_one({"userId": user_id}, {"$inc": {"AIRT": req.amount}})
        await db["airtime_token_operations"].update_one(
            {"_id": operation_id},
            {"$set": {"status": "FAILED", "error": str(exc), "updatedAt": datetime.utcnow()}},
        )
        await db["airtime_history"].insert_one({
            "_id": f"{operation_id}-FAILED",
            "user_id": user_id,
            "type": "AIRT Redemption",
            "amount": int(req.amount),
            "network": req.provider.upper(),
            "phone": req.phone.strip(),
            "status": "Failed",
            "timestamp": datetime.utcnow(),
            "error": str(exc),
        })
        await notify_user(
            db, user_id, "airtime", "error",
            "Airtime redemption failed",
            f"Your redemption of {int(req.amount)} KES to {req.phone.strip()} failed and AIRT was refunded. {exc}",
            extra={"amount": int(req.amount), "phone": req.phone.strip()},
        )
        raise HTTPException(status_code=502, detail=f"Airtime delivery failed; AIRT refunded: {exc}")

    provider_id = result.get("receipt_id", operation_id)
    await db["airtime_token_operations"].update_one(
        {"_id": operation_id},
        {"$set": {"status": "CONFIRMED", "providerReference": provider_id, "confirmedAt": datetime.utcnow()}},
    )
    await db["airtime_history"].insert_one({
        "_id": operation_id,
        "user_id": user_id,
        "type": "AIRT Redemption",
        "amount": int(req.amount),
        "network": req.provider.upper(),
        "phone": req.phone.strip(),
        "status": "Completed",
        "timestamp": datetime.utcnow(),
        "providerReference": provider_id,
    })
    await notify_user(
        db, user_id, "airtime", "success",
        "Airtime redeemed",
        f"{int(req.amount)} KES of airtime was sent to {req.phone.strip()}.",
        extra={"amount": int(req.amount), "phone": req.phone.strip()},
    )
    return {
        "status": "success",
        "operationId": operation_id,
        "providerReference": provider_id,
        "amount": int(req.amount),
        "message": f"{int(req.amount)} AIRT redeemed as airtime to {req.phone.strip()}.",
    }
