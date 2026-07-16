from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from datetime import datetime
import uuid
import requests

from database import get_db
from services.safaricom_daraja import DarajaService

router = APIRouter(prefix="/api/ramp", tags=["Ramp & Swaps"])
mam_laka = DarajaService()

class RampExecute(BaseModel):
    direction: str
    channel: str
    from_asset: str
    to_asset: str
    amount: float
    rate: float
    fee: float
    counterparty: str

@router.post("/execute", status_code=201)
async def execute_ramp(body: RampExecute, db=Depends(get_db)):
    current_user = {"_id": "test_user_123"}
    trade_id = f"TRADE_{uuid.uuid4().hex[:8].upper()}"
    
    # Hardcoded for testing to prevent cache crash
    bid_rate = 128.00
    ask_rate = 132.00
    receive = 0.0

    # ========================================================
    # 🟢 ON-RAMP (DEPOSIT KES VIA STK PUSH)
    # ========================================================
    if body.direction == "on" and body.channel == "Mobile Money":
        receive = round(body.amount / ask_rate, 4)
        print(f"🚀 Triggering Daraja STK Push for {body.amount} KES to {body.counterparty}")
        
        try:
            token = mam_laka.get_access_token()
            initiate_url = f"{mam_laka.base_url}/api/v1/mobile/initiate"
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            payload = {
                "impalaMerchantId": "meshex_sandbox",
                "displayName": "Mamlaka Deposit",
                "currency": "KES",
                "amount": int(body.amount),
                "payerPhone": body.counterparty, 
                "mobileMoneySP": "M-Pesa",
                "externalId": trade_id,
                # Replace with your actual live ngrok URL when testing webhooks!
                "callbackUrl": "https://hemathermal-ha-dextrously.ngrok-free.dev/api/ramp/b2c/result"
            }
            res = requests.post(initiate_url, json=payload, headers=headers)
            
            if res.status_code not in [200, 201]:
                print(f"❌ Mam-laka Error: {res.text}")
                raise Exception("Mam-laka STK Push failed to initiate.")
                
        except Exception as e:
            print(f"❌ STK Error: {str(e)}")
            raise HTTPException(status_code=400, detail="Failed to connect to M-Pesa. Please try again.")

        doc = {
            "_id": trade_id, "direction": body.direction, "channel": body.channel,
            "fromAsset": body.from_asset, "toAsset": body.to_asset,
            "fromAmount": body.amount, "toAmount": receive,
            "status": "processing", "userId": current_user["_id"],
            "date": datetime.utcnow().strftime("%b %d, %Y"),
            "timeAgo": datetime.utcnow().strftime("%H:%M:%S")
        }
        await db["ramp_entries"].insert_one(doc)
        return {"id": trade_id, "status": "processing", "message": "STK Push sent!"}

    # ========================================================
    # 🔴 OFF-RAMP (WITHDRAW TO M-PESA VIA B2C)
    # ========================================================
    elif body.direction == "off" and body.channel == "Mobile Money":
        receive = round(body.amount * bid_rate, 2)
        print(f"🚀 Triggering Mam-laka Payout: {receive} KES to {body.counterparty}")
        
        try:
            token = mam_laka.get_access_token()
            payout_url = f"{mam_laka.base_url}/api/v1/mobile/transfer"
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            payload = {
                "impalaMerchantId": "meshex_sandbox",
                "currency": "KES",
                "amount": int(receive),
                "recipientPhone": body.counterparty,
                "mobileMoneySP": "M-Pesa",
                "externalId": trade_id,
                "callbackUrl": "https://hemathermal-ha-dextrously.ngrok-free.dev/api/ramp/b2c/result"
            }
            res = requests.post(payout_url, json=payload, headers=headers)
            
            if res.status_code not in [200, 201]:
                 raise Exception("M-Pesa Payout rejected by Sandbox.")
                 
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

        doc = {
            "_id": trade_id, "direction": body.direction, "channel": body.channel,
            "fromAsset": body.from_asset, "toAsset": body.to_asset,
            "fromAmount": body.amount, "toAmount": receive,
            "status": "processing", "userId": current_user["_id"],
            "date": datetime.utcnow().strftime("%b %d, %Y"),
            "timeAgo": datetime.utcnow().strftime("%H:%M:%S")
        }
        await db["ramp_entries"].insert_one(doc)
        return {"id": trade_id, "receive": receive, "status": "processing", "message": "Withdrawal sent to M-Pesa!"}

    return {"id": trade_id, "receive": receive, "status": "processing"}

@router.post("/b2c/result") 
async def mamlaka_stk_callback(payload: dict, db=Depends(get_db)):
    external_id = payload.get("externalId", "") 
    if external_id.startswith("TRADE_"):
        await db["ramp_entries"].update_one(
            {"_id": external_id}, {"$set": {"status": "completed"}}
        )
    return {"status": "acknowledged"}

@router.get("/history")
async def get_ramp_history(db=Depends(get_db)):
    current_user = {"_id": "test_user_123"}
    cursor = db["ramp_entries"].find({"userId": current_user["_id"]}).sort("_id", -1).limit(20)
    entries = await cursor.to_list(length=20)
    
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
            "timeAgo": e.get("timeAgo", "Recently")
        })
    return {"status": "success", "entries": formatted_entries}