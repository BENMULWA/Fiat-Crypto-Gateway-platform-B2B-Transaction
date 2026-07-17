from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from jose import jwt, JWTError
from broadcast import broadcast_manager
import json
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
from routes.auth import SECRET_KEY, ALGORITHM, get_current_user
from database import get_db

router = APIRouter(prefix="/api/retail", tags=["Retail User"])

@router.get("/wallet")
async def get_retail_wallet_balances(db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = safe_object_id(current_user.get("_id"))
    wallet = await db["retail_wallets"].find_one({"userId": user_id})
    
    if not wallet:
        balances = {"USDA": 0.0, "KES": 0.0, "IMP": 0.0}
    else:
        balances = {
            "USDA": float(wallet.get("USDA", 0.0)),
            "KES": float(wallet.get("KES", 0.0)),
            "IMP": float(wallet.get("IMP", 0.0)),
        }
    return {"status": "success", "balances": balances}

@router.websocket("/ws/retail")
async def retail_ws(websocket: WebSocket):
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001)
        return

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            await websocket.close(code=4002)
            return
    except JWTError:
        await websocket.close(code=4003)
        return

    await websocket.accept()

    async def ws_send(message: dict):
        try:
            await websocket.send_text(json.dumps(message))
        except Exception:
            pass

    await broadcast_manager.connect(user_id, ws_send)
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(json.dumps({"type": "pong", "received": data}))
    except WebSocketDisconnect:
        await broadcast_manager.disconnect(user_id, ws_send)


class ProfileUpdate(BaseModel):
    name: str
    email: str
    phone: str

@router.put("/profile")
async def update_retail_profile(profile: ProfileUpdate):
    print(f"Backend received profile update: {profile.name}, {profile.email}, {profile.phone}")
    return {"status": "success", "message": "Profile updated successfully"}


class KycSubmission(BaseModel):
    fullName: str
    idNumber: str
    email: str
    phone: str
    documentName: str | None = None

@router.post("/kyc/submit")
async def submit_kyc(payload: KycSubmission, db=Depends(get_db), current_user=Depends(get_current_user)):
    # Safely convert ID so MongoDB actually updates the document!
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