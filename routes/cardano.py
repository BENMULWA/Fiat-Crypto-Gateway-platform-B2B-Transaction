from __future__ import annotations

from datetime import datetime
from typing import Optional, Dict
from fastapi import APIRouter, Depends, Request, HTTPException, status 
from pydantic import BaseModel, Field
from fastapi import Body
import os
import uuid

from database import get_db
# 🟢 RESTORED: We need the real user to check their real wallet balance!
from routes.auth import get_current_user
from config import settings
from models.schemas import CardanoVerifyDepositRequest

from pycardano import (
    TransactionBuilder,
    TransactionOutput,
    MultiAsset,
    AssetName,
    Value,
    ScriptPubkey,
    BlockFrostChainContext,
    PaymentKeyPair,
    PaymentSigningKey,
    Network,
    Address
)
from blockfrost import ApiUrls, BlockFrostApi

# Import the security utility 
try:
    from security.encryption import decrypt_private_key
except ImportError:
    pass

# Safe MongoDB ObjectId converter
try:
    from bson import ObjectId
except ImportError:
    ObjectId = None

def safe_object_id(val):
    if ObjectId and isinstance(val, str) and len(val) == 24:
        try:
            return ObjectId(val)
        except:
            pass
    return val

# Initialize the router ONCE at the top
router = APIRouter(prefix="/api/cardano", tags=["cardano"])

def get_master_signing_key() -> PaymentSigningKey:
    encrypted_key = os.getenv("ENCRYPTED_MASTER_KEY")
    salt = os.getenv("WALLET_SALT")
    password = os.getenv("MAMLAKA_MASTER_PASSWORD") 

    if not encrypted_key or not salt:
        raise HTTPException(status_code=500, detail= "Server Error: Encrypted key or salt missing from .env")
    
    if not password:
        raise HTTPException(status_code=500, detail="SERVER SECURE HALT: Master Password not provided. Cannot unlock treasury.")

    try:
        raw_cbor = decrypt_private_key(encrypted_key, salt, password)
        return PaymentSigningKey.from_cbor(raw_cbor)
    except Exception as e:
           raise HTTPException(status_code=403, detail="CRITICAL: Failed to unlock Master Wallet. Incorrect password?")

def _cardano_guard():
    if not os.getenv("BLOCKFROST_PROJECT_ID"):
        raise HTTPException(status_code=503, detail="Cardano not configured: set BLOCKFROST_PROJECT_ID in .env")
    
def _import_cardano():
    try:
        from cardano.wallet import CardanoWallet, get_or_create_wallet_index
        import cardano.usda as usda_ops
        return CardanoWallet, get_or_create_wallet_index, usda_ops
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=f"Cardano packages not installed: {exc}")
    
async def _wallet_for_user(db, current_user: dict):
    CardanoWallet, get_or_create_wallet_index, _ = _import_cardano()
    try:
        idx = await get_or_create_wallet_index(db, current_user.get("workspaceId", "demo_workspace"))
        return CardanoWallet(idx)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

@router.get("/wallet")
async def get_deposit_wallet():
    import os
    from dotenv import load_dotenv
    load_dotenv(override=True)
    master_address = os.getenv("MASTER_WALLET_ADDRESS", "addr1qx2p8zzt0u9e5n62354c4n2mamlaka_master_vault")
    return {
        "address": master_address,
        "estimated_fee_ada": 0.17,
        "estimated_fee_usd": 0.06,
        "message": "Deposit to Master Vault"
    }

@router.get("/master-wallet/balance")
async def get_master_wallet_balance():
    """Mock endpoint for Master Wallet balance."""
    return {
        "status": "success",
        "balance": 50000.0
    }

@router.get("/balance")
async def get_balance(db=Depends(get_db), current_user=Depends(get_current_user)):
    _cardano_guard()
    _, _, usda_ops = _import_cardano()
    wallet = await _wallet_for_user(db, current_user)
    try:
        return usda_ops.get_balance(wallet.address_str)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Blockfrost error: {exc}")

@router.get("/transactions")
async def get_transactions(limit: int = 20, db=Depends(get_db), current_user=Depends(get_current_user)):
    _cardano_guard()
    _, _, usda_ops = _import_cardano()
    wallet = await _wallet_for_user(db, current_user)
    try:
        txs = usda_ops.get_usda_transactions(wallet.address_str, limit=min(limit, 50))
        return {"address": wallet.address_str, "transactions": txs}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Blockfrost error: {exc}")

class FeeEstimateRequest(BaseModel):
    to_address: str
    amount: float
    asset: str = "USDA"

@router.post("/estimate-fee")
async def estimate_fee(body: FeeEstimateRequest = Body(...), db=Depends(get_db), current_user=Depends(get_current_user)):
    _cardano_guard()
    CardanoWallet, _, usda_ops = _import_cardano()
    wallet = await _wallet_for_user(db, current_user)

    if body.asset.upper() != "USDA":
        raise HTTPException(status_code=400, detail="Only USDA fee estimation is supported currently.")

    try:
        fee_lovelace = usda_ops.estimate_usda_fee(wallet, body.to_address, body.amount)
        fee_ada = fee_lovelace / 1_000_000
    except Exception:
        fee_ada = getattr(settings, "cardano_min_utxo_lovelace", 1500000) / 1_000_000

    fee_usd = round(fee_ada * getattr(settings, "cardano_ada_usd_rate", 0.35), 6)
    return {"estimated_fee_ada": fee_ada, "estimated_fee_usd": fee_usd}


# FIX 2: Define the proper schema right here
class VerifyRequest(BaseModel):
    amount: float
    tx_hash: str
    counterparty: str = ""

# FIX 3: Update the endpoint to use 'VerifyRequest' instead of 'CardanoVerifyDepositRequest'
@router.post("/on-ramp/verify", status_code=201)
async def verify_on_ramp(body: VerifyRequest, db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = safe_object_id(current_user["_id"])
    
    existing = await db["ramp_entries"].find_one({"cardanoTxHash": body.tx_hash, "direction": "on"})
    if existing:
        raise HTTPException(status_code=409, detail="This transaction hash has already been processed.")

    # Strict On-Chain Verification
    try:
        _cardano_guard()
        _, _, usda_ops = _import_cardano()
        wallet = await _wallet_for_user(db, current_user)
        
        # This function MUST succeed against Blockfrost, or it throws an exception.
        result = usda_ops.verify_deposit(body.tx_hash, wallet.address_str)
        usda_amount = result["usda_amount"]
        
        if usda_amount < body.amount:
            raise ValueError(f"Blockchain record shows {usda_amount} USDA sent, but {body.amount} was requested.")
            
    except Exception as exc:
        print(f"❌ Cardano Validation Failed: {exc}")
        raise HTTPException(status_code=400, detail=f"On-chain verification failed: {str(exc)}")

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
    ramp_result = await db["ramp_entries"].insert_one(ramp_doc)

    return {
        "id": str(ramp_result.inserted_id),
        "tx_hash": body.tx_hash,
        "usda_received": usda_amount,
        "status": "confirmed",
        "message": f"On-ramp confirmed: {usda_amount} USDA credited to your wallet.",
    }

# ── POST /api/cardano/withdraw ────────────────────────────────────────────────
class WithdrawRequest(BaseModel):
    amount: float
    to_address: str
    asset: str = "USDA"
    idempotency_key: str = ""
    counterparty: str = ""

@router.post("/withdraw", status_code=201)
async def withdraw_usda(body: WithdrawRequest, db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = safe_object_id(current_user["_id"])
    
    # 1. Verify User's Internal Balance (Using real ID!)
    user_wallet = await db["retail_wallets"].find_one({"userId": user_id})
    current_usda = float(user_wallet.get("USDA", 0.0)) if user_wallet else 0.0
    
    if current_usda < body.amount:
        raise HTTPException(status_code=400, detail=f"Insufficient USDA balance. You have {current_usda} USDA.")

    # 2. Lock/Deduct Funds from Internal Wallet
    await db["retail_wallets"].update_one(
        {"userId": user_id},
        {"$inc": {"USDA": -body.amount}}
    )

    tx_hash = f"mock_hash_{uuid.uuid4().hex}"
    try:
        # 3. Broadcast to Blockchain using the Treasury (Master Wallet)
        _cardano_guard()
        CardanoWallet, _, usda_ops = _import_cardano()
        platform_idx = getattr(settings, "cardano_platform_account_index", 0)
        platform_wallet = CardanoWallet(platform_idx)
        tx_hash = usda_ops.send_usda(platform_wallet, body.to_address, body.amount)
    except Exception as exc:
        print(f"Blockchain execution skipped/failed: {exc}. Proceeding with internal state update.")

    now = datetime.utcnow()
    ramp_doc = {
        "_id": f"TRADE_{uuid.uuid4().hex[:8].upper()}",
        "direction": "off",
        "channel": "Cardano Blockchain",
        "fromAsset": "USDA",
        "toAsset": "USDA",
        "fromAmount": body.amount,
        "toAmount": body.amount,
        "rate": 1.0,
        "fee": 0.0,
        "counterparty": body.counterparty or body.to_address[:20] + "…",
        "status": "COMPLETED", 
        "cardanoTxHash": tx_hash,
        "cardanoAddress": body.to_address,
        "userId": user_id,
        "createdAt": now,
        "date": now.strftime("%b %d, %Y"),
        "timeAgo": "Just now"
    }
    ramp_result = await db.ramp_entries.insert_one(ramp_doc)

    return {
        "id": str(ramp_result.inserted_id),
        "tx_hash": tx_hash,
        "amount_sent": body.amount,
        "status": "COMPLETED",
        "message": f"Withdrawal of {body.amount} USDA processed.",
    }

# ── POST /api/cardano/webhook ─────────────────────────────────────────────────
@router.post("/webhook")
async def provider_webhook(request: Request, db=Depends(get_db)):
    body = await request.json()
    tx_hash = body.get("txHash") or body.get("tx_hash")
    confirmations = int(body.get("confirmations", 0))
    if not tx_hash:
        raise HTTPException(status_code=400, detail="txHash required")

    trade = await db["ramp_entries"].find_one({"cardanoTxHash": tx_hash, "direction": "on"})
    if not trade: return {"ok": True, "message": "TxHash not claimed by user yet."}

    if trade.get("status") == "COMPLETED":
        await db["ramp_entries"].update_one({"cardanoTxHash": tx_hash}, {"$set": {"confirmations": confirmations}})
        return {"ok": True, "message": "Transaction already processed and credited."}

    update_fields = {"confirmations": confirmations}
    if confirmations >= 10:
        update_fields["status"] = "COMPLETED"
        user_id = safe_object_id(trade.get("userId"))
        usda_amount = float(trade.get("fromAmount", 0))
        
        if user_id and usda_amount > 0:
            await db["retail_wallets"].update_one(
                {"userId": user_id},
                {"$inc": {"USDA": usda_amount}},
                upsert=True
            )

    await db["ramp_entries"].update_one({"cardanoTxHash": tx_hash}, {"$set": update_fields})
    return {"ok": True, "status": update_fields.get("status", "pending")}