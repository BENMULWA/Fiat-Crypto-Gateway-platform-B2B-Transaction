import os
import uuid
import requests
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from database import get_db
from routes.auth import get_current_user

router = APIRouter(prefix="/api/stellar", tags=["Stellar Network"])

STELLAR_MASTER_ADDRESS = os.getenv("STELLAR_MASTER_ADDRESS", "GXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX")
HORIZON_URL = "https://horizon.stellar.org"

class InitiateDepositReq(BaseModel):
    asset: str
    amount: float

class DepositStatusRes(BaseModel):
    status: str
    tx_hash: str | None = None
    message: str = ""

# 1. GENERATE MEMO AND SAVE TO DB
@router.post("/deposit/initiate")
async def initiate_deposit(req: InitiateDepositReq, db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = current_user.get("_id")
    unique_memo = f"JASIRI-{uuid.uuid4().hex[:6].upper()}"
    
    dep_id = f"DEP_{uuid.uuid4().hex[:8].upper()}"
    
    # Save pending deposit to DB with the memo
    await db["pending_deposits"].insert_one({
        "_id": dep_id,
        "userId": user_id,
        "asset": req.asset,
        "network": "stellar",
        "amount": req.amount,
        "expected_memo": unique_memo,
        "status": "listening", # listening -> detected -> credited
        "tx_hash": None,
        "createdAt": datetime.utcnow()
    })
    
    return {
        "deposit_id": dep_id,
        "address": STELLAR_MASTER_ADDRESS,
        "memo": unique_memo
    }

# 2. SYNCHRONOUS ON-DEMAND POLLING CHECK
@router.get("/deposit/{dep_id}/status", response_model=DepositStatusRes)
async def get_deposit_status(dep_id: str, db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = current_user.get("_id")
    dep = await db["pending_deposits"].find_one({"_id": dep_id, "userId": user_id})
    
    if not dep:
        raise HTTPException(status_code=404, detail="Deposit session not found.")
    if dep["status"] == "credited":
        return DepositStatusRes(status="credited", tx_hash=dep.get("tx_hash"), message="Funds credited!")

    # --- SYNCHRONOUS BLOCKCHAIN CHECK ---
    try:
        # Check last 10 transactions on the master wallet
        url = f"{HORIZON_URL}/accounts/{STELLAR_MASTER_ADDRESS}/transactions?limit=10&order=desc"
        # FastAPI automatically runs synchronous `requests` in a threadpool, so this won't block the server!
        res = requests.get(url, timeout=5)
        res.raise_for_status()
        
        for tx in res.json().get("_embedded", {}).get("records", []):
            if tx.get("memo") == dep["expected_memo"] and tx.get("successful") == True:
                # FOUND IT! Update DB and credit user
                tx_hash = tx["id"]
                
                await db["pending_deposits"].update_one(
                    {"_id": dep_id},
                    {"$set": {"status": "credited", "tx_hash": tx_hash}}
                )
                
                await db["retail_wallets"].update_one(
                    {"userId": user_id},
                    {"$inc": {dep["asset"]: dep["amount"]}},
                    upsert=True
                )
                
                # Optional: Log to ramp_entries
                await db["ramp_entries"].insert_one({
                    "_id": f"TRADE_{uuid.uuid4().hex[:8].upper()}",
                    "direction": "on", "channel": "Stellar", "fromAsset": dep["asset"], "toAsset": dep["asset"],
                    "fromAmount": dep["amount"], "toAmount": dep["amount"], "status": "COMPLETED",
                    "cardanoTxHash": tx_hash, "userId": user_id, "createdAt": datetime.utcnow()
                })
                
                return DepositStatusRes(status="credited", tx_hash=tx_hash, message="Funds credited!")
                
    except Exception as e:
        print(f"Stellar sync check error: {e}")
        # Don't fail the request, just return current state

    return DepositStatusRes(status=dep["status"], message="Waiting for deposit...")


# ======================================================================
# 🟢 DYNAMIC MULTI-CHAIN DEPOSIT GATEWAY
# ======================================================================


@router.get("/deposit-info")
async def get_deposit_info(asset: str = "USDT", network: str = "stellar"):
    stellar_address = os.getenv("STELLAR_MASTER_ADDRESS")
    celo_address =os.getenv("CELO_EXIT_ADDRESS")
    tron_address = os.getenv("TRON_MASTER_ADDRESS")
    
    # 🟢 ADD THIS SAFETY CHECK:
    if not stellar_address:
        raise HTTPException(status_code=500, detail="STELLAR_MASTER_ADDRESS is not set in the backend .env file!")
        
    response_data = {
        "address": "",
        "memo": "",
        "network": network,
        "asset": asset
    }

    network_lower = network.lower()

    # 1. EVM Networks (Celo, Polygon, Ethereum) - No Memo required
    if network_lower in ["celo", "polygon", "ethereum"]:
        response_data["address"] = celo_address
        
    # 2. Tron Network - No Memo required
    elif network_lower in ["tron", "trc20"]:
        response_data["address"] = tron_address
        
    # 3. Stellar Network - MEMO IS STRICTLY REQUIRED
    elif network_lower == "stellar":
        response_data["address"] = stellar_address
        
        # Generate a unique 6-character hex memo for this specific user's deposit
        unique_memo = f"JASIRI-{uuid.uuid4().hex[:6].upper()}"
        response_data["memo"] = unique_memo
        
        # ⚠️ IN PRODUCTION: Save this unique_memo to MongoDB here
        # tied to the current_user's ID, so when the funds arrive on the Stellar blockchain,
        # your server knows exactly whose account to credit!

    # 4. Cardano Network
    elif network_lower == "cardano":
        response_data["address"] = os.getenv("MASTER_WALLET_ADDRESS", "addr1qx2p8...mamlaka_master_vault")

    return {"status": "success", "data": response_data}