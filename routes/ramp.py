from fastapi import APIRouter, Depends, HTTPException, Request
from datetime import datetime
import uuid

# Assuming you have these defined in your project
from database import get_db
from models.schemas import RampExecute 
from auth import get_current_user
from services.safaricom_daraja import DarajaService

router = APIRouter(prefix="/api/ramp", tags=["ramp"])

daraja = DarajaService()

# --- 1. THE TRIGGER (From React to Backend) ---
@router.post("/execute", status_code=201)
async def execute_ramp(body: RampExecute, db=Depends(get_db), current_user=Depends(get_current_user)):
    """
    Executes a swap. Automatically routes to Payout (Off-Ramp) or Collection (On-Ramp).
    """
    receive = round(body.amount * body.rate - body.fee, 4)
    
    # Generate a unique Trade ID for tracking with Lipad
    trade_id = f"TRADE_{uuid.uuid4().hex[:8].upper()}"

    doc = {
        "_id": trade_id, # Use our generated ID as the Mongo ID so Lipad can reference it
        "direction": body.direction,
        "channel": body.channel,
        "fromAsset": body.from_asset,
        "toAsset": body.to_asset,
        "fromAmount": body.amount,
        "toAmount": receive,
        "rate": body.rate,
        "fee": body.fee,
        "counterparty": body.counterparty, # This is the phone number
        "status": "processing", # Always starts as processing until webhook confirms
        "workspaceId": current_user["workspaceId"],
        "userId": current_user["_id"],
        "createdAt": datetime.utcnow(),
    }
    
    # Insert the pending record into MongoDB
    await db.ramp_entries.insert_one(doc)
    
    if body.channel == "Mobile Money":
        if body.direction == "off":
            # OFF-RAMP: We are sending KES to the user (Payout)
            kes_payout = int(receive)
            daraja_response = daraja.execute_b2c_payout(
                phone_number=body.counterparty,
                amount=kes_payout,
                transaction_id=trade_id
            )
        elif body.direction == "on":
            # ON-RAMP: We are requesting KES from the user (STK Push Collection)
            kes_collection = int(body.amount)
            daraja_response = daraja.execute_c2b_collection(
                phone_number=body.counterparty,
                amount=kes_collection,
                transaction_id=trade_id
            )
            
        if daraja_response.get("status") == "error":
            # If Lipad rejects it immediately, update DB and throw error to frontend
            await db.ramp_entries.update_one({"_id": trade_id}, {"$set": {"status": "failed", "error": daraja_response.get("message")}})
            raise HTTPException(status_code=400, detail=daraja_response.get("message"))
            
        return {
            "id": trade_id, 
            "receive": receive, 
            "status": "processing",
            "message": "M-Pesa transaction initiated. Waiting for network confirmation."
        }

    # If it's not Mobile Money, return normally
    return {"id": trade_id, "receive": receive, "status": "processing"}

@router.get("/history")
async def get_ramp_history(db=Depends(get_db), current_user=Depends(get_current_user)):
    """
    Fetches the transaction history for the user's dashboard.
    """
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

    return {"entries": entries}

# --- 2. THE WEBHOOKS (From Lipad back to Backend) ---

@router.post("/b2c/result")
async def lipad_result_webhook(request: Request, db=Depends(get_db)):
    """
    Lipad calls this URL automatically ~5 seconds after a successful or failed payout/collection.
    It passes the 'externalId' back to us, which matches our Mongo '_id'.
    """
    payload = await request.json()
    
    # Extract Lipad's specific payload fields based on the API docs
    status = payload.get("transactionStatus")
    external_id = payload.get("externalId") # This is our custom trade_id
    
    if status == "COMPLETE":
        print(f"✅ Trade {external_id} completed successfully!")
        print(f"💰 Net Amount delivered: {payload.get('netAmount')} {payload.get('currency')}")
        
        # Update the database record to 'completed'
        await db.ramp_entries.update_one(
            {"_id": external_id}, 
            {"$set": {"status": "completed"}}
        )
    else:
        # Failed (e.g., User cancelled STK push, or M-Pesa is full)
        error_msg = payload.get("reason", "No reason provided by Lipad")
        print(f"❌ Trade {external_id} failed: {error_msg}")
        
        # Update the database record to 'failed'
        await db.ramp_entries.update_one(
            {"_id": external_id}, 
            {"$set": {"status": "failed", "error": error_msg}}
        )
        
    # Acknowledge receipt to Lipad so they stop retrying
    return {"status": "Acknowledged"}