"""
This file handles the internal ledger, fiat operations,and internal swaps( like swapping KES- USDA)
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from datetime import datetime
import uuid

from database import get_db

router = APIRouter(prefix="/api/ramp", tags=["Ramp & Swaps"])

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
    """
    Executes an internal swap and pushes it to the HFT ledger.
    DEV MODE: Authentication bypassed completely!
    """
    # 1. HARDCODED MOCK USER
    current_user = {"_id": "test_user_123"}
    
    receive = round(body.amount * body.rate - body.fee, 4)
    trade_id = f"TRADE_{uuid.uuid4().hex[:8].upper()}"

    if body.direction == "swap":
        # 🚀 THE BRIDGE: Inject into the HFT Immutable Ledger!
        # This makes it show up on the Admin Execution Tape instantly
        ledger_entry = {
            "txn_id": trade_id,
            "timestamp": datetime.utcnow(),
            "from_node": "RETAIL_SPOKE",
            "to_node": "TREASURY_HUB",
            "asset": body.from_asset,
            "amount": body.amount,
            # Normalize to USD value roughly for the ledger
            "internal_usd_value": body.amount / 130.50 if "KES" in body.from_asset else body.amount,
            "txn_type": "RETAIL_SWAP"
        }
        await db["transactions"].insert_one(ledger_entry)
        
        # Also log the history for the retail user's UI
        doc = {
            "_id": trade_id,
            "direction": body.direction,
            "channel": body.channel,
            "fromAsset": body.from_asset,
            "toAsset": body.to_asset,
            "fromAmount": body.amount,
            "toAmount": receive,
            "status": "completed", 
            "userId": current_user["_id"],
            "date": datetime.utcnow().strftime("%b %d, %Y"),
            "timeAgo": "Just now"
        }
        await db["ramp_entries"].insert_one(doc)

        return {
            "id": trade_id, 
            "receive": receive, 
            "status": "completed", 
            "message": "Internal Swap Executed & Routed to Treasury!"
        }

    return {"id": trade_id, "receive": receive, "status": "processing"}


@router.get("/history")
async def get_ramp_history(db=Depends(get_db)):
    """
    Fetches the retail user's swap history.
    DEV MODE: Authentication bypassed completely!
    """
    current_user = {"_id": "test_user_123"}
    
    cursor = db["ramp_entries"].find({"userId": current_user["_id"]}).sort("_id", -1).limit(20)
    entries = await cursor.to_list(length=20)
    
    # Format for the UI
    formatted_entries = []
    for e in entries:
        formatted_entries.append({
            "id": str(e["_id"]),
            "direction": e.get("direction", "swap"),
            "channel": e.get("channel", "Internal Ledger"),
            "fromAsset": e.get("fromAsset", "KES"),
            "toAsset": e.get("toAsset", "USDA"),
            "fromAmount": e.get("fromAmount", 0),
            "toAmount": e.get("toAmount", 0),
            "status": e.get("status", "completed"),
            "date": e.get("date", "Today"),
            "timeAgo": e.get("timeAgo", "Recently")
        })
        
    return {"status": "success", "entries": formatted_entries}

# --- 2. THE WEBHOOKS (From Lipad back to Backend) ---

@router.post("/b2c/result")
async def lipad_result_webhook(request: Request, db=Depends(get_db)):
    """
    Lipad calls this URL automatically ~5 seconds after a successful or failed payout/collection.
    It passes the 'externalId' back to us, which matches our Mongo '_id'.
    """
    payload = await request.json()
    print("🔔 WEBHOOK RECEIVED FROM LIPAD:", payload) # Helpful for debugging!
    
    # Extract Lipad's specific payload fields based on the API docs
    # Different Lipad endpoints sometimes use 'status' instead of 'transactionStatus'
    status = payload.get("transactionStatus") or payload.get("status") 
    external_id = payload.get("externalId") 
    
    # Check for success (Handling different variations Lipad might send)
    if status in ["COMPLETE", "COMPLETED", "Success", "Successful"]:
        
        # 1. Find the pending trade in the database
        trade = await db.ramp_entries.find_one({"_id": external_id})
        
        if trade and trade.get("status") != "completed":
            print(f"✅ Trade {external_id} completed successfully!")
            
            # 2. Update the receipt status to 'completed'
            await db.ramp_entries.update_one(
                {"_id": external_id}, 
                {"$set": {"status": "completed"}}
            )
            
            # 3. CRITICAL: Update the user's actual Wallet Balance!
            user_id = trade["userId"]
            
            # Robust search handling both String and ObjectId formats
            search_conditions = [{"userId": user_id}, {"userId": str(user_id)}]
            try:
                if ObjectId.is_valid(str(user_id)):
                    search_conditions.append({"userId": ObjectId(str(user_id))})
            except Exception:
                pass
                
            user_wallet = await db.wallets.find_one({"$or": search_conditions})
            
            # Auto-provision if missing
            if not user_wallet:
                new_wallet = {"userId": user_id, "balances": {"KES": 5000, "USDA": 0, "UGX": 0}}
                result = await db.wallets.insert_one(new_wallet)
                wallet_id = result.inserted_id
            else:
                wallet_id = user_wallet["_id"]

            if trade["direction"] == "on":
                # ON-RAMP: User deposited KES, give them USDA
                await db.wallets.update_one(
                    {"_id": wallet_id},
                    {"$inc": {f"balances.{trade['toAsset']}": trade["toAmount"]}}
                )
                print(f"💰 Credited {trade['toAmount']} {trade['toAsset']} to User Wallet")
                
            elif trade["direction"] == "off":
                # OFF-RAMP: User withdrew USDA for KES, deduct USDA
                await db.wallets.update_one(
                    {"_id": wallet_id},
                    {"$inc": {f"balances.{trade['fromAsset']}": -trade["fromAmount"]}}
                )
                print(f"📉 Deducted {trade['fromAmount']} {trade['fromAsset']} from User Wallet")
                
    else:
        # Failed (e.g., User cancelled STK push, wrong PIN, or M-Pesa is full)
        error_msg = payload.get("reason", "No reason provided by Lipad")
        print(f"❌ Trade {external_id} failed: {error_msg}")
        
        # Update the database record to 'failed'
        await db.ramp_entries.update_one(
            {"_id": external_id}, 
            {"$set": {"status": "failed", "error": error_msg}}
        )
        
    # Acknowledge receipt to Lipad with a 200 OK so they stop retrying
    return {"status": "Acknowledged"}

# Excute ramp on