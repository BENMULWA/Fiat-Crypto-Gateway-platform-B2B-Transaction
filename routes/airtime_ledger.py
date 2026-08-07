import uuid
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from database import get_db
# ADDED: Import authentication to know WHO is making the request
from routes.auth import get_current_user
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
        "internal_airt": live_artm, 
        "internal_imp": live_artm   
    }

# 🟢 FIXED: Secured History Endpoint
@router.get("/history")
async def get_history(db=Depends(get_db), current_user=Depends(get_current_user)):
    """Fetches airtime tokenization history ONLY for the logged-in user."""
    # 1. Get the real User ID from the JWT token
    user_id = current_user.get("_id")
    
    # 2. Query MongoDB strictly for THIS user's data
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
            # ADDED: React UI needs this exact "status" key to show the green/red badges
            "status": h.get("status", "Completed"), 
            "time": h.get("timestamp").strftime("%b %d, %H:%M") if h.get("timestamp") else "Just now"
        })
    return {"status": "success", "history": formatted}

@router.post("/mint")
async def mint_imp(req: MintRequest, db=Depends(get_db)):
    """Legacy Minting Route (Kept for UI compatibility)"""
    return {"status": "success", "message": f"Minted {req.amount} IMP"}

# 🟢 FIXED: Secured Redeem Endpoint
@router.post("/redeem")
async def redeem_airtime(req: RedeemRequest, db=Depends(get_db), current_user=Depends(get_current_user)):
    """DIRECT WITHDRAWAL: Disburses physical Airtime bypassing internal DB limits."""
    
    # 1. Get the real User ID
    user_id = current_user.get("_id")
    
    txn_id = uuid.uuid4().hex[:8].upper()
    provider = req.provider.upper()
    
    if provider not in ["AIRTEL", "SAFARICOM", "TELKOM"]:
        provider = "AIRTEL"
        
    print(f"🚀 ATTEMPTING DIRECT LIVE WITHDRAWAL: {req.amount} KES to {req.phone} via {provider} for User: {user_id}")
        
    # 2. Trigger Mam-laka External API Directly
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
    
    # 3. Log History on Success (Using REAL user_id instead of "test_user_123")
    await db["airtime_history"].insert_one({
        "user_id": user_id, # FIXED: Now tied to the actual user
        "type": "Direct Withdraw",
        "amount": req.amount,
        "usd": -(req.amount / 130.5), 
        "network": provider,
        "country": "Kenya",
        "status": "Completed", # ADDED: So React knows it succeeded
        "timestamp": datetime.utcnow()
    })
    
    return {"status": "success", "message": f"Redeemed {req.amount} Airtime to {req.phone}"}

@router.post("/reset-wallet")
async def reset_wallet(db=Depends(get_db)):
    # Legacy route
    return {"status": "success", "message": "Wallet reset."}