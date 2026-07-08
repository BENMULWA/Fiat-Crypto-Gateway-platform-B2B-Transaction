from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import uuid
from typing import Optional
from datetime import datetime
from database import get_db

# Import our Mam-laka service
from services.safaricom_daraja import DarajaService

router = APIRouter(prefix="/api/airtime", tags=["Airtime Ledger"])
mam_laka = DarajaService()

# --- Pydantic Models for Security ---
class MintRequest(BaseModel):
    amount: float
    network: str
    country: str
    note: Optional[str] = ""

class RedeemRequest(BaseModel):
    amount: float
    phone: str
    provider: str
    user_id: Optional[str] = "test_user_123" 

@router.post("/mint")
async def mint_airtime(req: MintRequest, db=Depends(get_db)):
    txn_id = f"MINT-{uuid.uuid4().hex[:8].upper()}"
    
    doc = {
        "txn_id": txn_id,
        "timestamp": datetime.utcnow(),
        "type": "mint",
        "amount": req.amount,
        "network": req.network,
        "country": req.country,
        "usd": req.amount,
        "status": "completed"
    }
    await db["airtime_history"].insert_one(doc)
    
    return {
        "status": "success", 
        "message": f"Successfully minted {req.amount} IMP",
        "tx_id": txn_id
    }

@router.post("/redeem")
async def redeem_airtime(req: RedeemRequest, db=Depends(get_db)):
    # 1. INTERNAL BALANCE CHECK
    user_wallet = await db["user_wallets"].find_one({"_id": req.user_id})
    balances = user_wallet.get("balances", {}) if user_wallet else {}
    current_airt_balance = balances.get("AIRT", 0)
    
    if current_airt_balance < req.amount:
        raise HTTPException(
            status_code=400, 
            detail=f"Insufficient Internal Airtime. You have {current_airt_balance} AIRT, but tried to redeem {req.amount}."
        )

    txn_id = f"AIRT-{uuid.uuid4().hex[:8].upper()}"
    
    print(f"🚀 Triggering Live Airtime Disbursement: {req.amount} to {req.phone} via {req.provider}")
    
    # 2. TRIGGER MAM-LAKA API
    result = mam_laka.disburse_airtime(
        phone_number=req.phone,
        amount=int(req.amount),
        transaction_id=txn_id,
        provider=req.provider
    )
    
    # 3. HANDLE MAM-LAKA ERRORS
    if result.get("status") == "error":
        # 🚨 FIX: LOG THE FAILED ATTEMPT TO HISTORY BEFORE RAISING ERROR!
        failed_doc = {
            "txn_id": txn_id,
            "timestamp": datetime.utcnow(),
            "type": "failed",
            "amount": req.amount,
            "network": req.provider,
            "country": "Kenya", 
            "usd": 0.0,
            "status": "failed",
            "error": result.get("message", "Unknown API error")
        }
        await db["airtime_history"].insert_one(failed_doc)
        
        # Now raise the error to frontend
        raise HTTPException(status_code=400, detail=result.get("message"))
        
    # 4. ATOMIC DEDUCTION (Only happens on success)
    await db["user_wallets"].update_one(
        {"_id": req.user_id},
        {"$inc": {"balances.AIRT": -req.amount}}
    )
        
    # 5. LOG SUCCESS TO HISTORY
    doc = {
        "txn_id": txn_id,
        "timestamp": datetime.utcnow(),
        "type": "redeem",
        "amount": req.amount,
        "network": req.provider,
        "country": "Kenya", 
        "usd": -req.amount,
        "status": "completed"
    }
    await db["airtime_history"].insert_one(doc)

    return {
        "status": "success", 
        "message": "Airtime successfully sent to phone!",
        "provider_receipt": result
    }

@router.get("/summary")
async def get_airtime_summary():
    balance_res = mam_laka.get_merchant_balance()
    artm_balance = 0.0
    
    if balance_res.get("status") == "success":
        data = balance_res.get("data", {})
        artm_balance = data.get("artmBalance", 0.0)
        
    return {
        "status": "success",
        "live_artm_balance": artm_balance
    }

@router.get("/history")
async def get_airtime_history(db=Depends(get_db)):
    cursor = db["airtime_history"].find().sort("timestamp", -1).limit(10)
    records = await cursor.to_list(length=10)
    
    history = []
    for r in records:
        time_str = r["timestamp"].strftime("%b %d, %H:%M")
        history.append({
            "id": r["txn_id"],
            "type": r.get("type", "unknown"),
            "amount": r["amount"],
            "network": r["network"],
            "phone": r.get("phone", None),
            "country": r.get("country", "KE"),
            "time": time_str,
            "usd": r.get("usd", 0.0),
            # Pass the error message to frontend if it's a failed transaction
            "error": r.get("error", None)
        })
        
    return {"status": "success", "history": history}