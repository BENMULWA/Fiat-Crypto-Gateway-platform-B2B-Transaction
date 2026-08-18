from __future__ import annotations

import os
import uuid
import requests
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, Request, HTTPException, Body
from pydantic import BaseModel
from database import get_db
from routes.auth import get_current_user
from config import settings

try:
    from bson import ObjectId
except ImportError:
    ObjectId = None

def safe_object_id(val):
    if ObjectId and isinstance(val, str) and len(val) == 24:
        try:
            return ObjectId(val)
        except Exception:
            pass
    return val

router = APIRouter(prefix="/api/cardano", tags=["Cardano Blockchain"])

# --- 1. BLOCKFROST CONFIGURATION ---
BLOCKFROST_PROJECT_ID = os.getenv("BLOCKFROST_PROJECT_ID")
BLOCKFROST_URL = "https://cardano-mainnet.blockfrost.io/api/v0"
BLOCKFROST_HEADERS = {"project_id": BLOCKFROST_PROJECT_ID} if BLOCKFROST_PROJECT_ID else {}

def _cardano_guard():
    if not BLOCKFROST_PROJECT_ID:
        raise HTTPException(
            status_code=503, 
            detail="Cardano service unavailable: BLOCKFROST_PROJECT_ID is not configured in .env"
        )

# --- 2. SCHEMAS ---
class WithdrawRequest(BaseModel):
    amount: float
    to_address: Optional[str] = None
    toAddress: Optional[str] = None
    address: Optional[str] = None
    asset: str = "USDA"
    idempotency_key: str = ""
    counterparty: str = ""

    def get_destination(self) -> str:
        dest = self.to_address or self.toAddress or self.address
        if not dest or not dest.strip():
            raise HTTPException(status_code=400, detail="Destination Cardano address is required.")
        return dest.strip()

class FeeEstimateRequest(BaseModel):
    to_address: Optional[str] = None
    toAddress: Optional[str] = None
    address: Optional[str] = None
    amount: float
    asset: str = "USDA"

class VerifyRequest(BaseModel):
    amount: float
    tx_hash: str
    counterparty: str = ""

# --- 3. HELPER IMPORTS ---
def _import_cardano():
    try:
        from cardano.wallet import CardanoWallet, get_or_create_wallet_index
        import cardano.usda as usda_ops
        return CardanoWallet, get_or_create_wallet_index, usda_ops
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=f"Cardano module error: {exc}")

async def _wallet_for_user(db, current_user: dict):
    CardanoWallet, get_or_create_wallet_index, _ = _import_cardano()
    try:
        idx = await get_or_create_wallet_index(db, current_user.get("workspaceId", "demo_workspace"))
        return CardanoWallet(idx)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

# --- 4. ENDPOINTS ---

@router.get("/wallet")
async def get_deposit_wallet(db=Depends(get_db), current_user=Depends(get_current_user)):
    """Returns a unique Cardano deposit address for the user."""
    _cardano_guard()
    try:
        wallet = await _wallet_for_user(db, current_user)
        return {
            "address": wallet.address_str,
            "message": "Unique deposit address ready."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/estimate-fee")
async def estimate_fee(body: FeeEstimateRequest = Body(...), db=Depends(get_db), current_user=Depends(get_current_user)):
    """Estimates on-chain transaction fees for USDA transfers."""
    _cardano_guard()
    CardanoWallet, _, usda_ops = _import_cardano()
    wallet = await _wallet_for_user(db, current_user)

    destination = body.to_address or body.toAddress or body.address or wallet.address_str

    try:
        fee_lovelace = usda_ops.estimate_usda_fee(wallet, destination, body.amount)
        fee_ada = fee_lovelace / 1_000_000
    except Exception:
        fee_ada = getattr(settings, "cardano_min_utxo_lovelace", 1500000) / 1_000_000

    fee_usd = round(fee_ada * getattr(settings, "cardano_ada_usd_rate", 0.35), 4)
    # Default minimum network fee for USDA transfers
    standard_usda_fee = 0.17

    return {
        "estimated_fee_ada": fee_ada,
        "estimated_fee_usd": fee_usd,
        "network_fee_usda": standard_usda_fee
    }
@router.post("/withdraw", status_code=201)
async def withdraw_usda(body: WithdrawRequest, db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = safe_object_id(current_user["_id"])
    dest_address = body.get_destination()
    withdraw_amount = float(body.amount)

    if withdraw_amount <= 0:
        raise HTTPException(status_code=400, detail="Withdrawal amount must be greater than zero.")

    # 1. Check user retail balance
    user_wallet = await db["retail_wallets"].find_one({"userId": user_id})
    user_usda = float(user_wallet.get("USDA", 0.0)) if user_wallet else 0.0

    if user_usda < withdraw_amount:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient USDA balance. Available: {user_usda:.4f} USDA, Requested: {withdraw_amount:.4f} USDA."
        )

    # 2. Atomically lock and deduct user balance
    deduct_result = await db["retail_wallets"].update_one(
        {"userId": user_id, "USDA": {"$gte": withdraw_amount}},
        {"$inc": {"USDA": -withdraw_amount}}
    )
    if deduct_result.modified_count == 0:
        raise HTTPException(status_code=400, detail="Balance update failed. Please try again.")

    # 3. Broadcast on-chain via Blockfrost
    tx_hash = None
    try:
        _cardano_guard()
        CardanoWallet, _, usda_ops = _import_cardano()
        platform_idx = getattr(settings, "cardano_platform_account_index", 0)
        platform_wallet = CardanoWallet(platform_idx)
        tx_hash = usda_ops.send_usda(platform_wallet, dest_address, withdraw_amount)
    except Exception as exc:
        # Refund on failure
        await db["retail_wallets"].update_one({"userId": user_id}, {"$inc": {"USDA": withdraw_amount}})
        raise HTTPException(status_code=502, detail=f"Cardano on-chain error: {str(exc)}")

    # 4. Insert ramp ledger entry
    now = datetime.utcnow()
    ramp_doc = {
        "_id": f"TRADE_{uuid.uuid4().hex[:8].upper()}",
        "direction": "off",
        "channel": "Cardano Blockchain",
        "fromAsset": "USDA",
        "toAsset": "USDA",
        "fromAmount": withdraw_amount,
        "toAmount": max(0.0, withdraw_amount - 0.17),
        "rate": 1.0,
        "fee": 0.17,
        "counterparty": dest_address[:15] + "…" + dest_address[-6:],
        "status": "COMPLETED",
        "cardanoTxHash": tx_hash,
        "cardanoAddress": dest_address,
        "userId": user_id,
        "createdAt": now,
        "date": now.strftime("%b %d, %Y"),
        "timeAgo": "Just now"
    }
    await db["ramp_entries"].insert_one(ramp_doc)

    return {
        "tx_hash": tx_hash,
        "amount_sent": withdraw_amount,
        "status": "COMPLETED",
        "message": f"Successfully sent {withdraw_amount} USDA to {dest_address[:10]}…"
    }
    
    
@router.post("/on-ramp/verify", status_code=201)
async def verify_on_ramp(body: VerifyRequest, db=Depends(get_db), current_user=Depends(get_current_user)):
    """Verifies an incoming on-chain deposit transaction."""
    user_id = safe_object_id(current_user["_id"])
    
    existing = await db["ramp_entries"].find_one({"cardanoTxHash": body.tx_hash, "direction": "on"})
    if existing:
        raise HTTPException(status_code=409, detail="This transaction hash has already been credited.")

    try:
        _cardano_guard()
        _, _, usda_ops = _import_cardano()
        wallet = await _wallet_for_user(db, current_user)
        result = usda_ops.verify_deposit(body.tx_hash, wallet.address_str)
        usda_amount = float(result["usda_amount"])
        
        if usda_amount < body.amount:
            raise ValueError(f"On-chain deposit shows {usda_amount} USDA, but {body.amount} was submitted.")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Deposit verification failed: {str(exc)}")

    await db["retail_wallets"].update_one(
        {"userId": user_id},
        {"$inc": {"USDA": usda_amount}},
        upsert=True
    )

    now = datetime.utcnow()
    ramp_doc = {
        "_id": f"TRADE_{uuid.uuid4().hex[:8].upper()}",
        "direction": "on",
        "channel": "Cardano Blockchain",
        "fromAsset": "USDA",
        "toAsset": "USDA",
        "fromAmount": usda_amount,
        "toAmount": usda_amount,
        "rate": 1.0,
        "fee": 0.0,
        "counterparty": body.counterparty or "Cardano On-Chain",
        "status": "COMPLETED",
        "cardanoTxHash": body.tx_hash,
        "userId": user_id,
        "createdAt": now,
        "date": now.strftime("%b %d, %Y"),
        "timeAgo": "Just now"
    }
    await db["ramp_entries"].insert_one(ramp_doc)

    return {
        "status": "COMPLETED",
        "tx_hash": body.tx_hash,
        "usda_received": usda_amount,
        "message": f"Successfully credited {usda_amount} USDA."
    }
    
@router.get("/master-wallet/balance")
async def get_master_wallet_balance():
    """Fetches REAL balance of the master wallet via Blockfrost."""
    _cardano_guard()
    master_address = os.getenv("MASTER_WALLET_ADDRESS")
    if not master_address: 
        raise HTTPException(status_code=500, detail="Master wallet missing in .env")
    
    try:
        url = f"{BLOCKFROST_URL}/addresses/{master_address}"
        res = requests.get(url, headers=BLOCKFROST_HEADERS, timeout=5).json()
        
        # Handle Blockfrost 404 (Address never used on-chain)
        if "error" in res:
            return {"status": "success", "ada": 0.0, "usda": 0.0, "note": "Address empty/unused"}

        lovelace = 0
        usda_balance = 0.0

        for asset in res.get("amount", []):
            if asset["unit"] == "lovelace":
                lovelace = int(asset["quantity"])
            elif "55534441" in asset["unit"]: # USDA hex
                usda_balance = int(asset["quantity"]) / 1_000_000
                
        ada_balance = lovelace / 1_000_000
        
        return {
            "status": "success",
            "ada": ada_balance,
            "usda": usda_balance
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch master balance: {str(e)}")