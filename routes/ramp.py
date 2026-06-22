from fastapi import APIRouter, Depends
from database import get_db
from models.schemas import RampExecute
from auth import get_current_user
from datetime import datetime

router = APIRouter(prefix="/api/ramp", tags=["ramp"])


@router.post("/execute", status_code=201)
async def execute_ramp(body: RampExecute, db=Depends(get_db), current_user=Depends(get_current_user)):
    receive = round(body.amount * body.rate - body.fee, 4)
    doc = {
        "direction": body.direction,
        "channel": body.channel,
        "fromAsset": body.from_asset,
        "toAsset": body.to_asset,
        "fromAmount": body.amount,
        "toAmount": receive,
        "rate": body.rate,
        "fee": body.fee,
        "counterparty": body.counterparty,
        "status": "on" if body.direction == "on" else "off",
        "workspaceId": current_user["workspaceId"],
        "userId": current_user["_id"],
        "createdAt": datetime.utcnow(),
    }
    result = await db.ramp_entries.insert_one(doc)
    return {"id": str(result.inserted_id), "receive": receive, "status": doc["status"]}


@router.get("/history")
async def get_ramp_history(db=Depends(get_db), current_user=Depends(get_current_user)):
    workspace_id = current_user["workspaceId"]
    cursor = db.ramp_entries.find({"workspaceId": workspace_id}).sort("createdAt", -1).limit(50)
    entries = []
    async for r in cursor:
        created = r.get("createdAt")
        entries.append({
            "id": str(r["_id"]),
            "fromAsset": r["fromAsset"],
            "toAsset": r["toAsset"],
            "fromAmount": r["fromAmount"],
            "toAmount": r["toAmount"],
            "channel": r["channel"],
            "type": "On-Ramp" if r["direction"] == "on" else "Off-Ramp",
            "timeAgo": created.strftime("%-d %b") if created else "—",
            "status": r["status"],
        })

    if not entries:
        entries = [
            {"id": "1", "fromAsset": "KES", "toAsset": "USDA", "fromAmount": 3.0, "toAmount": 3.0, "channel": "Mobile Money", "type": "On-Ramp", "timeAgo": "2d Ago", "status": "on"},
            {"id": "2", "fromAsset": "USD", "toAsset": "KES", "fromAmount": 10.0, "toAmount": 10.0, "channel": "Mobile Money", "type": "On-Ramp", "timeAgo": "3d Ago", "status": "on"},
            {"id": "3", "fromAsset": "KES", "toAsset": "USDA", "fromAmount": 100.0, "toAmount": 100.0, "channel": "Mobile Money", "type": "On-Ramp", "timeAgo": "4d Ago", "status": "on"},
        ]
    return {"entries": entries}


from fastapi import APIRouter, Depends, HTTPException, Request
from database import get_db
from models.schemas import RampExecute
from auth import get_current_user
from datetime import datetime
import uuid

from services.safaricom_daraja import DarajaService

router = APIRouter(prefix="/api/ramp", tags=["ramp"])

# Initialize the Safaricom engine
daraja = DarajaService()

@router.post("/execute", status_code=201)
async def execute_ramp(body: RampExecute, db=Depends(get_db), current_user=Depends(get_current_user)):
    # Calculate exact payout
    receive = round(body.amount * body.rate - body.fee, 4)
    
    # Generate a unique custom trade ID for Daraja tracking
    trade_id = f"TRADE_{uuid.uuid4().hex[:8].upper()}"
    
    # Base document for MongoDB
    doc = {
        "_id": trade_id, # Override Mongo's default ObjectId with our Trade ID
        "direction": body.direction,
        "channel": body.channel,
        "fromAsset": body.from_asset,
        "toAsset": body.to_asset,
        "fromAmount": body.amount,
        "toAmount": receive,
        "rate": body.rate,
        "fee": body.fee,
        "counterparty": body.counterparty, # e.g., the M-Pesa phone number "2547XXXXXXXX"
        "status": "pending",
        "workspaceId": current_user["workspaceId"],
        "userId": current_user["_id"],
        "createdAt": datetime.utcnow(),
        "providerId": None # Safaricom's Conversation ID will go here
    }

    if body.direction == "off" and "mobile" in body.channel.lower():
        # Safaricom B2C requires integers (no decimal KES)
        kes_payout = int(receive)
        
        # Fire the Safaricom Engine!
        daraja_response = daraja.execute_b2c_payout(
            phone_number=body.counterparty, 
            amount=kes_payout,
            transaction_id=trade_id
        )
        
        if daraja_response.get("status") == "error":
            raise HTTPException(status_code=400, detail=daraja_response.get("message"))
            
        # Success: Safaricom accepted the request. Mark as processing.
        doc["status"] = "processing"
        doc["providerId"] = daraja_response.get("provider_id")
    else:
        # Standard behavior for On-Ramps or non-mobile channels
        doc["status"] = "on" if body.direction == "on" else "off"

    await db.ramp_entries.insert_one(doc)
    
    return {"id": trade_id, "receive": receive, "status": doc["status"]}


@router.get("/history")
async def get_ramp_history(db=Depends(get_db), current_user=Depends(get_current_user)):
    workspace_id = current_user["workspaceId"]
    cursor = db.ramp_entries.find({"workspaceId": workspace_id}).sort("createdAt", -1).limit(50)
    entries = []
    
    async for r in cursor:
        created = r.get("createdAt")
        entries.append({
            "id": str(r["_id"]),
            "fromAsset": r["fromAsset"],
            "toAsset": r["toAsset"],
            "fromAmount": r["fromAmount"],
            "toAmount": r["toAmount"],
            "channel": r["channel"],
            "type": "On-Ramp" if r["direction"] == "on" else "Off-Ramp",
            "timeAgo": created.strftime("%-d %b") if created else "—",
            "status": r["status"],
        })

    if not entries:
        entries = [
            {"id": "1", "fromAsset": "KES", "toAsset": "USDA", "fromAmount": 3.0, "toAmount": 3.0, "channel": "Mobile Money", "type": "On-Ramp", "timeAgo": "2d Ago", "status": "on"},
        ]
    return {"entries": entries}


# --- THE WEBHOOKS (From Safaricom to Backend) ---
@router.post("/b2c/result")
async def b2c_result_webhook(request: Request, db=Depends(get_db)):
    """
    Safaricom calls this URL automatically ~5 seconds after a payout.
    """
    payload = await request.json()
    
    result = payload.get("Result", {})
    result_code = result.get("ResultCode")
    trade_id = result.get("OriginatorConversationID") 
    
    if result_code == 0:
        # Success: Money arrived on the user's phone!
        print(f"✅ Trade {trade_id} completed successfully!")
        
        # Update MongoDB doc status to completed
        await db.ramp_entries.update_one(
            {"_id": trade_id},
            {"$set": {"status": "completed"}}
        )
    else:
        # Failed: User's M-Pesa is full, or invalid number
        error_msg = result.get("ResultDesc")
        print(f"❌ Trade {trade_id} failed: {error_msg}")
        
        # Update MongoDB doc status to failed
        await db.ramp_entries.update_one(
            {"_id": trade_id},
            {"$set": {"status": "failed", "errorReason": error_msg}}
        )
        
    return {"ResultCode": 0, "ResultDesc": "Accepted"}

@router.post("/b2c/timeout")
async def b2c_timeout_webhook(request: Request):
    payload = await request.json()
    print("⚠️ Safaricom Timeout:", payload)
    return {"ResultCode": 0, "ResultDesc": "Accepted"}