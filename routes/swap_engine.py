import asyncio
import os
import uuid
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel
from typing import Optional
from database import get_db
from routes.auth import get_verified_current_user
from notifications import notify_user
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from dotenv import load_dotenv
import httpx

try:
    from bson import ObjectId
except ImportError:
    ObjectId = None

load_dotenv(override=True)

router = APIRouter(prefix="/api/swap", tags=["Real-Time Swap Engine"])

# ==========================================
# 1. ASSET CONFIGURATION (Matches Frontend)
# ==========================================
ASSET_CONFIG = {
    "USDA": {"type": "crypto"}, "USDC": {"type": "crypto"}, "USDT": {"type": "crypto"}, "cUSD": {"type": "crypto"}, "USD": {"type": "fiat"},
    "KES": {"type": "fiat"}, "UGX": {"type": "fiat"}, "TZS": {"type": "fiat"}, "RWF": {"type": "fiat"}, "BIF": {"type": "fiat"},
    "XAF": {"type": "fiat"}, "XOF": {"type": "fiat"},
    "AIRT": {"type": "telco"}, "IMP": {"type": "internal"}
}

# ==========================================
# 2. PRODUCTION BLOCKCHAIN & API CONFIG
# ==========================================
# Celo Configuration for swap logic
CELO_RPC = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
CELO_CHAIN_ID = 42220


# this creates a real celo web3 transfer logic
w3_celo = Web3(Web3.HTTPProvider(CELO_RPC, request_kwargs={'timeout': 15}))
w3_celo.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

CELO_ASSET_CONTRACTS = {
    "cUSD": w3_celo.to_checksum_address("0x765DE816845861e75A25fCA122bb6898B8B1282a"), 
    "USDC": w3_celo.to_checksum_address("0xcebA9300f2b948710d2653dD7B07f33A8B32118C"), 
    "USDT": w3_celo.to_checksum_address("0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e")  
}

CELO_ERC20_ABI = [
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
]

# Telco & Payment APIs
AFRICASTALKING_API_KEY = os.getenv("AFRICASTALKING_API_KEY", "")
AFRICASTALKING_USERNAME = os.getenv("AFRICASTALKING_USERNAME", "")
FLUTTERWAVE_API_KEY = os.getenv("FLUTTERWAVE_API_KEY", "")

# ==========================================
# 3. DATA MODELS
# ==========================================
class ExecuteSwapReq(BaseModel):
    from_asset: str
    to_asset: str
    from_amount: float
    destination_address: Optional[str] = None  
    destination_phone: Optional[str] = None  

class SwapResponse(BaseModel):
    swap_id: str
    status: str
    message: str
    tx_hash: Optional[str] = None

# ==========================================
# 4. PRODUCTION SETTLEMENT FUNCTIONS
# ==========================================

async def settle_crypto_on_celo(to_address: str, asset: str, amount: float) -> dict:
    """
    🔵 PRODUCTION: Send real crypto on Celo blockchain
    Uses Treasury wallet to execute actual ERC20 transfers.
    """
    try:
        private_key = os.getenv("CELO_TREASURY_PK")
        if not private_key:
            raise ValueError("CELO_TREASURY_PK not configured")
        
        account = w3_celo.eth.account.from_key(private_key if private_key.startswith("0x") else f"0x{private_key}")
        
        if asset not in CELO_ASSET_CONTRACTS:
            raise ValueError(f"Unsupported Celo asset: {asset}")
        
        # Validate destination
        try:
            to_address = w3_celo.to_checksum_address(to_address.strip())
        except Exception:
            raise ValueError(f"Invalid destination address: {to_address}")
        
        # Convert amount to base units
        decimals = 18 if asset == "cUSD" else 6
        amount_base = int(amount * (10 ** decimals))
        
        # Build and execute transfer
        contract = w3_celo.eth.contract(address=CELO_ASSET_CONTRACTS[asset], abi=CELO_ERC20_ABI)
        nonce = w3_celo.eth.get_transaction_count(account.address)
        
        tx = contract.functions.transfer(to_address, amount_base).build_transaction({
            'chainId': CELO_CHAIN_ID,
            'gas': 150000,
            'gasPrice': w3_celo.eth.gas_price,
            'nonce': nonce,
        })
        
        signed_tx = w3_celo.eth.account.sign_transaction(tx, account.key)
        raw_tx = getattr(signed_tx, 'raw_transaction', getattr(signed_tx, 'rawTransaction', None))
        tx_hash = w3_celo.eth.send_raw_transaction(raw_tx)
        tx_hex = w3_celo.to_hex(tx_hash)
        
        print(f"✅ [CELO] Sent {amount} {asset} to {to_address}")
        print(f"   TX Hash: {tx_hex}")
        
        # Wait for confirmation (up to 30 seconds)
        receipt = await asyncio.wait_for(
            asyncio.to_thread(lambda: w3_celo.eth.wait_for_transaction_receipt(tx_hex, timeout=30)),
            timeout=35.0
        )
        
        if receipt['status'] != 1:
            raise Exception(f"Transaction failed on-chain: {tx_hex}")
        
        return {
            "success": True,
            "tx_hash": tx_hex,
            "status": "completed",
            "block": receipt['blockNumber'],
            "gas_used": receipt['gasUsed']
        }
        
    except Exception as e:
        print(f"❌ [CELO] Transfer failed: {str(e)}")
        return {"success": False, "error": str(e)}


async def settle_airtime_via_africastalking(phone_number: str, amount_kes: float) -> dict:
    """
    🔴 PRODUCTION: Send real airtime via Africa's Talking API
    Dispatches to Safaricom, Airtel, Vodafone (East Africa)
    """
    try:
        if not AFRICASTALKING_API_KEY or not AFRICASTALKING_USERNAME:
            raise ValueError("Africa's Talking credentials not configured")
        
        # Normalize phone number
        phone = phone_number.strip()
        if not phone.startswith("+"):
            phone = f"+{phone}"
        
        # Africa's Talking Airtime API
        url = "https://api.africastalking.com/airtime/send"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Bearer {AFRICASTALKING_API_KEY}"
        }
        
        
        # Convert KES to airtime amount (depends on provider)
        # Typically 1 KES ≈ 1 airtime unit
        airtime_amount = int(amount_kes)
        
        payload = {
            "username": AFRICASTALKING_USERNAME,
            "recipients": f"{phone},{airtime_amount}"
        }
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, headers=headers, data=payload)
            result = response.json()
        
        if response.status_code != 200:
            raise Exception(f"Africa's Talking API error: {result}")
        
        # Check response status
        if result.get("numSent") > 0:
            print(f"✅ [AIRTIME] Sent {amount_kes} KES airtime to {phone}")
            return {
                "success": True,
                "phone": phone,
                "amount_kes": amount_kes,
                "reference": result.get("entries", [{}])[0].get("requestId", "AT_REF"),
                "status": "completed"
            }
        else:
            error_msg = result.get("entries", [{}])[0].get("errorMessage", "Unknown error")
            raise Exception(f"Airtime dispatch failed: {error_msg}")
        
    except Exception as e:
        print(f"❌ [AIRTIME] Dispatch failed: {str(e)}")
        return {"success": False, "error": str(e)}


async def settle_mobilemoney_via_flutterwave(phone_number: str, currency: str, amount: float) -> dict:
    """
    💳 PRODUCTION: Send real fiat via Flutterwave Mobile Money
    Supports KES, UGX, TZS, XOF, XAF, RWF, BIF, etc.
    """
    try:
        if not FLUTTERWAVE_API_KEY:
            raise ValueError("Flutterwave API key not configured")
        
        phone = phone_number.strip()
        if not phone.startswith("+"):
            phone = f"+{phone}"
        
        # Flutterwave Transfer API
        url = "https://api.flutterwave.com/v3/transfers"
        headers = {
            "Authorization": f"Bearer {FLUTTERWAVE_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # Map currency to Flutterwave codes
        currency_map = {
            "KES": "KES", "UGX": "UGX", "TZS": "TZS", "RWF": "RWF",
            "BIF": "BIF", "XAF": "XAF", "XOF": "XOF", "ZAR": "ZAR"
        }
        
        fw_currency = currency_map.get(currency, currency)
        
        payload = {
            "account_number": phone,
            "amount": amount,
            "currency": fw_currency,
            "narration": "Crypto-Meshex Withdrawal",
            "reference": f"MM-{uuid.uuid4().hex[:8].upper()}",
            "callback_url": os.getenv("FLUTTERWAVE_CALLBACK_URL", ""),
            "meta": {
                "platform": "meshex"
            }
        }
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            result = response.json()
        
        if response.status_code not in [200, 201]:
            raise Exception(f"Flutterwave error: {result.get('message', 'Unknown error')}")
        
        if result.get("status") == "success":
            print(f"✅ [MOBILE MONEY] Sent {amount} {currency} to {phone}")
            return {
                "success": True,
                "phone": phone,
                "currency": currency,
                "amount": amount,
                "reference": result.get("data", {}).get("reference", "FLW_REF"),
                "status": "completed",
                "transfer_id": result.get("data", {}).get("id")
            }
        else:
            raise Exception(f"Flutterwave transfer failed: {result.get('message')}")
        
    except Exception as e:
        print(f"❌ [MOBILE MONEY] Dispatch failed: {str(e)}")
        return {"success": False, "error": str(e)}


# ==========================================
# 5. BACKEND RATE CALCULATOR
# ==========================================
async def calculate_backend_rate(from_asset: str, to_asset: str, db) -> float:
    """
    Calculate secure backend rate (never trust frontend)
    Uses treasury rate book.
    """
    from routes.treasury import compute_swap_quote_from_book, get_or_create_rate_book
    rate_book = await get_or_create_rate_book(db)
    return compute_swap_quote_from_book(from_asset, to_asset, 1, rate_book)["execution_rate"]


def validate_swap_request(req: ExecuteSwapReq) -> None:
    """Validate the request before balance checks or settlement calls."""
    if req.from_asset == req.to_asset:
        raise ValueError("Cannot swap to the same asset.")

    to_asset_config = ASSET_CONFIG.get(req.to_asset)
    if not to_asset_config:
        raise ValueError(f"Unsupported target asset: {req.to_asset}")

    if to_asset_config["type"] == "crypto" and (
        not req.destination_address or not req.destination_address.startswith("0x")
    ):
        raise ValueError("Valid 0x wallet address required for crypto.")

    if to_asset_config["type"] == "telco" and (
        not req.destination_phone or len(req.destination_phone.strip()) < 10
    ):
        raise ValueError("Valid phone number required for Airtime.")

    if to_asset_config["type"] == "fiat" and req.to_asset != req.from_asset:
        if not req.destination_phone or len(req.destination_phone.strip()) < 10:
            raise ValueError("Valid phone number required for Mobile Money.")


# ==========================================
# 6. THE MAIN PRODUCTION SWAP ENDPOINT
# ==========================================
@router.post("/execute", response_model=SwapResponse)
async def execute_production_swap(req: ExecuteSwapReq, db=Depends(get_db), current_user=Depends(get_verified_current_user)):
    """
    🔵 PRODUCTION SWAP - Real on-chain and API settlements
    No simulations. Actual funds flow immediately.
    """
    try:
        validate_swap_request(req)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    # Never accept a client-supplied user id for a funds-moving request.
    user_id = str(current_user["_id"])

    # 2. Calculate backend rate
    rate = await calculate_backend_rate(req.from_asset, req.to_asset, db)
    to_amount = req.from_amount * rate

    # 3. Check user balance
    user_id_candidates = [user_id]
    if ObjectId and len(user_id) == 24:
        try:
            user_id_candidates.append(ObjectId(user_id))
        except:
            pass
    
    user_wallet = await db["retail_wallets"].find_one({"userId": {"$in": user_id_candidates}})
    if not user_wallet:
        raise HTTPException(404, "User wallet not found.")
    
    # Use the ACTUAL userId from the found wallet for all subsequent operations
    actual_user_id = user_wallet.get("userId")
    
    current_balance = user_wallet.get(req.from_asset, 0.0)
    if current_balance < req.from_amount:
        raise HTTPException(400, f"Insufficient {req.from_asset} balance. You have {current_balance}.")

    # 4. Get asset configuration for settlement routing
    to_asset_config = ASSET_CONFIG.get(req.to_asset)
    if not to_asset_config:
        raise HTTPException(400, f"Unsupported asset: {req.to_asset}")

    # 4a. Create swap record
    swap_id = f"SWP-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{user_id[:8]}"
    
    await db["ramp_entries"].insert_one({
        "_id": swap_id,
        "userId": user_id,
        "fromAsset": req.from_asset,
        "toAsset": req.to_asset,
        "fromAmount": req.from_amount,
        "toAmount": to_amount,
        "status": "PROCESSING",
        "direction": "swap",
        "createdAt": datetime.utcnow()
    })

    # 5. Deduct from user wallet BEFORE settling (escrow pattern)
    await db["retail_wallets"].update_one(
        {"userId": actual_user_id},
        {"$inc": {req.from_asset: -req.from_amount}}
    )

    # 6. Execute settlement SYNCHRONOUSLY (no background tasks)
    settlement_result = None
    try:
        if to_asset_config["type"] == "crypto":
            settlement_result = await settle_crypto_on_celo(req.destination_address, req.to_asset, to_amount)
        
        elif to_asset_config["type"] == "telco":
            settlement_result = await settle_airtime_via_africastalking(req.destination_phone, req.from_amount)
        
        elif to_asset_config["type"] == "fiat" and req.to_asset != req.from_asset:
            settlement_result = await settle_mobilemoney_via_flutterwave(req.destination_phone, req.to_asset, to_amount)
        
        else:  # Internal transfer
            await db["retail_wallets"].update_one(
                {"userId": actual_user_id},
                {"$inc": {req.to_asset: to_amount}}
            )
            settlement_result = {"success": True, "status": "completed", "type": "internal"}

        # 7. Update swap record with result
        if settlement_result and settlement_result.get("success"):
            print(f"✅ [Swap {swap_id}] Settlement succeeded: {settlement_result}")
            
            await db["ramp_entries"].update_one(
                {"_id": swap_id},
                {"$set": {
                    "status": "completed",
                    "success": True,
                    "settledAt": datetime.utcnow(),
                    "txHash": settlement_result.get("tx_hash") or settlement_result.get("reference"),
                    "external_ref": settlement_result.get("reference") or settlement_result.get("transfer_id")
                }}
            )
            
            # ⚠️ CRITICAL: Only credit if not already settled on-chain (crypto)
            if to_asset_config["type"] != "crypto":
                # For fiat/airtime, we control the wallet, so credit now
                result = await db["retail_wallets"].update_one(
                    {"userId": actual_user_id},
                    {"$inc": {req.to_asset: to_amount}}
                )
                print(f"   Credited {to_amount} {req.to_asset} to wallet. Modified: {result.modified_count}")
            else:
                # For crypto, funds went to destination_address on-chain, not this wallet
                print(f"   Crypto transferred to {req.destination_address} on-chain")
            
            await notify_user(
                db, actual_user_id, "swap", "success",
                "Swap completed",
                f"{req.from_amount:g} {req.from_asset} → {to_amount:g} {req.to_asset} completed.",
                extra={"fromAsset": req.from_asset, "toAsset": req.to_asset, "entryId": str(swap_id)},
            )

            return SwapResponse(
                swap_id=swap_id,
                status="completed",
                message=f"Successfully swapped {req.from_amount} {req.from_asset} → {to_amount} {req.to_asset}",
                tx_hash=settlement_result.get("tx_hash") or settlement_result.get("reference")
            )
        else:
            error_msg = settlement_result.get("error") if settlement_result else "Settlement failed - no result"
            print(f"❌ [Swap {swap_id}] Settlement returned failure: {error_msg}")
            raise Exception(error_msg)
    
    except Exception as e:
        # ROLLBACK: Refund user immediately on failure
        print(f"🚨 [Swap {swap_id}] FAILED: {type(e).__name__}: {str(e)}")
        print(f"   Attempting rollback: Restoring {req.from_amount} {req.from_asset}...")
        
        try:
            # Restore deducted balance
            refund_result = await db["retail_wallets"].update_one(
                {"userId": actual_user_id},
                {"$inc": {req.from_asset: req.from_amount}}
            )
            print(f"   Refund: Modified {refund_result.modified_count} wallet docs")
            
            # Mark swap as failed
            fail_result = await db["ramp_entries"].update_one(
                {"_id": swap_id},
                {"$set": {
                    "status": "failed",
                    "success": False,
                    "error": str(e),
                    "failedAt": datetime.utcnow()
                }}
            )
            print(f"   Failure record: Updated {fail_result.modified_count} swap docs")
            
        except Exception as rollback_error:
            print(f"   ⚠️ CRITICAL: Rollback also failed! {str(rollback_error)}")

        await notify_user(
            db, actual_user_id, "swap", "error",
            "Swap failed",
            f"Your swap of {req.from_amount:g} {req.from_asset} to {req.to_asset} failed and was refunded. {str(e)}",
            extra={"fromAsset": req.from_asset, "toAsset": req.to_asset, "entryId": str(swap_id)},
        )

        raise HTTPException(502, f"Swap failed: {str(e)}")
