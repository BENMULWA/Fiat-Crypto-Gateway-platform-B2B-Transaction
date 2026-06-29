"""
This file handles the internal ledger, fiat operations,and internal swaps( like swapping KES- USDA)
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from datetime import datetime
import uuid
from pydantic import BaseModel
from bson import ObjectId

# Assuming you have these defined in your project
from database import get_db
from models.schemas import RampExecute 
from auth import get_current_user
from services.safaricom_daraja import DarajaService

router = APIRouter(prefix="/api/ramp", tags=["ramp"])

daraja = DarajaService()

# --- INTERNAL SWAP LOGIC ---

class SwapRequest(BaseModel):
    from_asset: str
    to_asset: str
    amount: float

def fetch_rate(from_asset: str, to_asset: str) -> float:
    # Mock exchange rates (In production, this would pull from Redis)
    rates = {
        "KES-USDA": 1/130,
        "USDA-KES": 130,
        "KES-UGX": 28.5,
        "UGX-KES": 1/28.5
    }
    key = f"{from_asset}-{to_asset}"
    return rates.get(key, 1.0)

@router.post("/swap", status_code=201)
async def execute_internal_swap(
    body: SwapRequest,
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    # 1. Fetch user's current internal balances
    user_id = current_user["_id"]
    
    # Robust search handling both String and ObjectId formats
    search_conditions = [{"userId": user_id}, {"userId": str(user_id)}]
    try:
        if ObjectId.is_valid(str(user_id)):
            search_conditions.append({"userId": ObjectId(str(user_id))})
    except Exception:
        pass
        
    user_wallet = await db.wallets.find_one({"$or": search_conditions})
    
    # AUTO-PROVISIONING: If no wallet exists, create a sandbox wallet automatically!
    if not user_wallet:
        print(f"Wallet not found for {user_id}. Auto-creating with 5000 KES test funds...")
        new_wallet = {
            "userId": user_id,
            "balances": {
                "KES": 5000,  # Seeding with 5000 KES for testing
                "USDA": 0,
                "UGX": 0
            }
        }
        result = await db.wallets.insert_one(new_wallet)
        new_wallet["_id"] = result.inserted_id
        user_wallet = new_wallet

    # 2. Check if they actually have enough of the "From" asset (e.g. KES)
    if user_wallet["balances"].get(body.from_asset, 0) < body.amount:
        raise HTTPException(status_code=400, detail=f"Insufficient {body.from_asset} balance.")

    # 3. Fetch the live exchange rate
    live_rate = fetch_rate(body.from_asset, body.to_asset)
    receive_amount = body.amount * live_rate

    # 4. ATOMIC DATABASE UPDATE (The actual swap)
    await db.wallets.update_one(
        {"_id": user_wallet["_id"]}, # Update the exact wallet we just found/created
        {
            "$inc": {
                f"balances.{body.from_asset}": -body.amount,      # Subtract From Asset
                f"balances.{body.to_asset}": receive_amount       # Add To Asset
            }
        }
    )

    # 5. Log the receipt into the History table
    ramp_doc = {
        "direction": "swap",
        "channel": "Internal Ledger",
        "fromAsset": body.from_asset,
        "toAsset": body.to_asset,
        "fromAmount": body.amount,
        "toAmount": receive_amount,
        "rate": live_rate,
        "status": "COMPLETED", 
        "workspaceId": current_user.get("workspaceId", "default"),
        "userId": current_user["_id"],
        "createdAt": datetime.utcnow(),
    }
    await db.ramp_entries.insert_one(ramp_doc)

    return {"status": "success", "message": "Swap completed instantly."}


# --- 1. THE TRIGGER (From React to Backend) ---
@router.post("/execute", status_code=201)
async def execute_ramp(body: RampExecute, db=Depends(get_db), current_user=Depends(get_current_user)):
    """
    Executes a ramp. Automatically routes to Payout (Off-Ramp), Collection (On-Ramp), or Internal Swap.
    """
    receive = round(body.amount * body.rate - body.fee, 4)
    
    # Generate a unique Trade ID for tracking with Lipad
    trade_id = f"TRADE_{uuid.uuid4().hex[:8].upper()}"

    # --- 1. INTERNAL SWAP INTERCEPTOR (Instant, Zero-Fee) ---
    if body.direction == "swap":
        # A) Update user wallet instantly (Atomic Transaction)
        await db.wallets.update_one(
            {"userId": current_user["_id"]},
            {
                "$inc": {
                    f"balances.{body.from_asset}": -body.amount,
                    f"balances.{body.to_asset}": receive
                }
            },
            upsert=True
        )
        
        # B) Save receipt instantly as "completed"
        doc = {
            "_id": trade_id,
            "direction": body.direction,
            "channel": body.channel,
            "fromAsset": body.from_asset,
            "toAsset": body.to_asset,
            "fromAmount": body.amount,
            "toAmount": receive,
            "rate": body.rate,
            "fee": body.fee,
            "counterparty": body.counterparty,
            "status": "completed", # <--- Instantly marked as completed
            "workspaceId": current_user["workspaceId"],
            "userId": current_user["_id"],
            "createdAt": datetime.utcnow(),
        }
        await db.ramp_entries.insert_one(doc)
        return {"id": trade_id, "receive": receive, "status": "completed", "message": "Internal Swap Executed instantly!"}

    # --- 2. ON/OFF RAMP LOGIC (Requires Webhooks) ---
    doc = {
        "_id": trade_id,
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
            "direction": r.get("direction", "on"),
            "type": "Swap" if r.get("direction") == "swap" else ("On-Ramp" if r.get("direction") == "on" else "Off-Ramp"),
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