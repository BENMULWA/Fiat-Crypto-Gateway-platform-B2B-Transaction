from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from datetime import datetime

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

@router.get("/wallet")
async def get_retail_wallet_balances(db=Depends(get_db), current_user=Depends(get_current_user)):
    # Safely convert the user ID to ObjectId before reading!
    user_id = safe_object_id(current_user.get("_id"))
    wallet = await db["retail_wallets"].find_one({"userId": user_id})
    
    # Dynamically pull all supported balances from the DB
    balances = {}
    for asset in SUPPORTED_ASSETS:
        balances[asset] = float(wallet.get(asset, 0.0)) if wallet else 0.0

    return {"status": "success", "balances": balances}

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

@router.post("/kyc/submit")
async def submit_kyc(payload: KycSubmission, db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = safe_object_id(current_user.get("_id"))

    await db["users"].update_one(
        {"_id": user_id},
        {"$set": {
            "kycStatus": "verified",
            "kycSubmittedAt": datetime.utcnow(),
            "kycDetails": {
                "fullName": payload.fullName,
                "idNumber": payload.idNumber,
                "email": payload.email,
                "phone": payload.phone,
                "documentName": payload.documentName or "identity-document"
            }
        }},
        upsert=True
    )
    return {"status": "success", "message": "KYC Identity Verified Successfully!", "kycStatus": "verified"}

@router.get("/kyc/status")
async def get_kyc_status(db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = safe_object_id(current_user.get("_id"))
    user = await db["users"].find_one({"_id": user_id}, {"kycStatus": 1, "kycDetails": 1})
    
    if not user:
        return {"status": "success", "kycStatus": "unverified", "kycDetails": {}}

    return {
        "status": "success",
        "kycStatus": user.get("kycStatus", "unverified"),
        "kycDetails": user.get("kycDetails", {})
    }