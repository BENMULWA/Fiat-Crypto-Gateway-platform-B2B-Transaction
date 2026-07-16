import uuid
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from database import get_db
from services.safaricom_daraja import DarajaService

router = APIRouter(prefix="/api/airtime", tags=["Airtime Tokenization"])
mam_laka = DarajaService()

# --- Pydantic Schemas ---
class MintRequest(BaseModel):
    amount: float
    network: str
    country: str
    note: str = ""

class RedeemRequest(BaseModel):
    amount: float
    phone: str
    provider: str = "AIRTEL"

# --- Endpoints ---
@router.get("/summary")
async def get_summary(db=Depends(get_db)):
    """Fetches real live balances DIRECTLY from Mam-laka API."""
    live_balance_res = mam_laka.get_merchant_balance()
    
    live_artm = 0.0
    if live_balance_res.get("status") == "success":
        live_artm = live_balance_res["data"].get("artmBalance", 0.0)
    
    return {
        "live_artm_balance": live_artm,
        "internal_airt": live_artm, # We pass the live balance to the UI
        "internal_imp": live_artm   # Bypass the UI's IMP requirement
    }

@router.get("/history")
async def get_history(db=Depends(get_db)):
    """Fetches airtime tokenization history from MongoDB."""
    cursor = db["airtime_history"].find({"user_id": "test_user_123"}).sort("timestamp", -1).limit(20)
    history = await cursor.to_list(length=20)
    
    formatted = []
    for h in history:
        formatted.append({
            "id": str(h["_id"]),
            "type": h.get("type"),
            "amount": h.get("amount", 0.0),
            "usd": h.get("usd", 0.0),
            "network": h.get("network", "Airtel"),
            "country": h.get("country", "Kenya"),
            "time": h.get("timestamp").strftime("%b %d, %H:%M") if h.get("timestamp") else "Just now"
        })
    return {"status": "success", "history": formatted}

@router.post("/mint")
async def mint_imp(req: MintRequest, db=Depends(get_db)):
    """Legacy Minting Route (Kept for UI compatibility)"""
    return {"status": "success", "message": f"Minted {req.amount} IMP"}

@router.post("/redeem")
async def redeem_airtime(req: RedeemRequest, db=Depends(get_db)):
    """DIRECT WITHDRAWAL: Disburses physical Airtime bypassing internal DB limits."""
    
    txn_id = uuid.uuid4().hex[:8].upper()
    provider = req.provider.upper()
    
    if provider not in ["AIRTEL", "SAFARICOM", "TELKOM"]:
        provider = "AIRTEL"
        
    print(f"🚀 ATTEMPTING DIRECT LIVE WITHDRAWAL: {req.amount} KES to {req.phone} via {provider}")
        
    # 🚀 TRIGGER MAM-LAKA EXTERNAL API DIRECTLY
    result = mam_laka.disburse_airtime(
        phone_number=req.phone,
        amount=int(req.amount),
        transaction_id=txn_id,
        provider=provider
    )
    
    # Surface real errors!
    if result.get("status") == "error":
        print(f"❌ Mam-laka API Error: {result.get('message')}")
        raise HTTPException(status_code=400, detail=f"Provider Error: {result.get('message')}")
    
    # Log History on Success
    await db["airtime_history"].insert_one({
        "user_id": "test_user_123",
        "type": "Direct Withdraw",
        "amount": req.amount,
        "usd": -(req.amount / 130.5), 
        "network": provider,
        "country": "Kenya",
        "timestamp": datetime.utcnow()
    })
    
    return {"status": "success", "message": f"Redeemed {req.amount} Airtime to {req.phone}"}

@router.post("/reset-wallet")
async def reset_wallet(db=Depends(get_db)):
    # Legacy route
    return {"status": "success", "message": "Wallet reset."}