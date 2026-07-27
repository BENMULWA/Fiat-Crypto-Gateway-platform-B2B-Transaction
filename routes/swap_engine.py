import asyncio
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel
from typing import Optional
from database import get_db

router = APIRouter(prefix="/api/swap", tags=["Real-Time Swap Engine"])

# ==========================================
# 1. ASSET CONFIGURATION (Matches Frontend)
# ==========================================
ASSET_CONFIG = {
    "USDA": {"type": "crypto"}, "USDC": {"type": "crypto"}, "USDT": {"type": "crypto"}, "USD": {"type": "fiat"},
    "KES": {"type": "fiat"}, "UGX": {"type": "fiat"}, "TZS": {"type": "fiat"}, "RWF": {"type": "fiat"}, "BIF": {"type": "fiat"},
    "XAF": {"type": "fiat"}, "XOF": {"type": "fiat"},
    "AIRT": {"type": "telco"}, "IMP": {"type": "internal"}
}

# ==========================================
# 2. DATA MODELS
# ==========================================
class ExecuteSwapReq(BaseModel):
    user_id: str
    from_asset: str
    to_asset: str
    from_amount: float
    destination_address: Optional[str] = None  
    destination_phone: Optional[str] = None  

class SwapResponse(BaseModel):
    swap_id: str
    status: str
    message: str

# ==========================================
# 3. BACKEND RATE CALCULATOR (SECURITY CRITICAL)
# ==========================================
def calculate_backend_rate(from_asset: str, to_asset: str) -> float:
    """
    NEVER trust the frontend rate. Calculate it here to prevent manipulation.
    In production, replace this with Redis/Binance API calls.
    """
    if from_asset == to_asset: return 1.0
    
    usd_base_rates = {
        "USDA": 1, "USDC": 1, "USDT": 1, "USD": 1, "IMP": 1, 
        "KES": 130.50, "UGX": 3750.00, "TZS": 2580.00,
        "RWF": 1320.00, "BIF": 2850.00, "XAF": 605.00, "XOF": 605.00, "AIRT": 130.50 
    }
    
    from_usd = usd_base_rates.get(from_asset, 1)
    to_usd = usd_base_rates.get(to_asset, 1)
    
    # Apply 2% spread
    return (from_usd / to_usd) * 0.98 

# ==========================================
# 4. EXTERNAL API PLACEHOLDERS
# ==========================================
async def send_real_celo_crypto(to_address: str, asset: str, amount: float):
    """TODO: Replace with your actual Celo Web3 logic from valora.py"""
    print(f"⛓️ [CELO] Sending {amount} {asset} to {to_address}...")
    await asyncio.sleep(5) # Simulate blockchain confirmation time
    # tx_hash = execute_celo_transfer(...)
    return "0xMOCK_TX_HASH_CELLO"

async def send_real_airtime(phone_number: str, amount_kes: float):
    """TODO: Replace with Africa's Talking or Reloadly API call"""
    print(f"📱 [TELCO] Dispatching KES {amount_kes} airtime to {phone_number}...")
    await asyncio.sleep(3) # Simulate Telco API
    # response = africastalking_client.send_airtime(phone_number, amount_kes)
    return "TELCO_REF_12345"

async def send_real_fiat_mobile_money(phone_number: str, currency: str, amount: float):
    """TODO: Replace with Flutterwave or Pawapay API call"""
    print(f"💸 [FIAT] Sending {amount} {currency} to {phone_number} via Mobile Money...")
    await asyncio.sleep(4) # Simulate Payment Gateway
    # response = flutterwave.initiate_transfer(phone_number, currency, amount)
    return "FLW_MOCK_REF"

# ==========================================
# 5. THE BACKGROUND SETTLEMENT WORKER
# ==========================================
async def settle_swap_in_background(swap_id: str, payload: dict, db):
    """
    This runs invisibly after the user gets a 200 OK response.
    It actually interacts with Celo, Telcos, and Banks.
    """
    to_asset = payload["to_asset"]
    to_amount = payload["to_amount"]
    asset_type = ASSET_CONFIG.get(to_asset, {}).get("type")
    ref_id = None

    try:
        # --- ROUTE 1: CRYPTO SETTLEMENT (e.g., KES -> USDA) ---
        if asset_type == "crypto":
            if not payload.get("destination_address"):
                raise Exception("Missing destination wallet address")
            ref_id = await send_real_celo_crypto(payload["destination_address"], to_asset, to_amount)

        # --- ROUTE 2: AIRTIME SETTLEMENT (e.g., KES -> AIRT) ---
        elif asset_type == "telco":
            if not payload.get("destination_phone"):
                raise Exception("Missing destination phone number")
            # Telcos usually deal in local fiat (KES), so we pass the equivalent KES amount
            ref_id = await send_real_airtime(payload["destination_phone"], payload["from_amount"])

        # --- ROUTE 3: FIAT SETTLEMENT (e.g., KES -> UGX or XOF) ---
        elif asset_type == "fiat" and to_asset != payload["from_asset"]:
            if not payload.get("destination_phone"):
                raise Exception("Missing phone number for Mobile Money disbursement")
            ref_id = await send_real_fiat_mobile_money(payload["destination_phone"], to_asset, to_amount)

        # --- ROUTE 4: INTERNAL SETTLEMENT ---
        else:
            print(f"🗃️ [INTERNAL] Crediting {to_amount} {to_asset} to DB")
            await db["user_wallets"].update_one(
                {"_id": payload["user_id"]},
                {"$inc": {f"balances.{to_asset}": to_amount}}
            )

        # ✅ SUCCESS: Update Database
        update_data = {"status": "COMPLETED", "settled_at": datetime.utcnow()}
        if ref_id: update_data["external_ref"] = ref_id
        
        await db["swaps"].update_one({"_id": swap_id}, {"$set": update_data})
        print(f"✅ [Swap {swap_id}] Successfully settled on external network!")

    except Exception as e:
        # 🚨 FAILURE: REFUND THE USER IMMEDIATELY
        print(f"🚨 [Swap {swap_id}] External settlement failed: {str(e)}. Refunding user...")
        
        await db["user_wallets"].update_one(
            {"_id": payload["user_id"]},
            {"$inc": {f"balances.{payload['from_asset']}": payload["from_amount"]}}
        )
        await db["swaps"].update_one(
            {"_id": swap_id}, 
            {"$set": {"status": "FAILED_REFUNDED", "error": str(e)}}
        )

# ==========================================
# 6. THE MAIN SWAP ENDPOINT (Called by React)
# ==========================================
@router.post("/execute", response_model=SwapResponse)
async def execute_real_time_swap(req: ExecuteSwapReq, background_tasks: BackgroundTasks, db=Depends(get_db)):
    
    # 1. Basic Validations
    if req.from_asset == req.to_asset:
        raise HTTPException(400, "Cannot swap to the same asset.")
    
    to_asset_config = ASSET_CONFIG.get(req.to_asset)
    if not to_asset_config:
        raise HTTPException(400, f"Unsupported target asset: {req.to_asset}")

    if to_asset_config["type"] == "crypto" and (not req.destination_address or not req.destination_address.startswith("0x")):
        raise HTTPException(400, "Valid 0x wallet address is required to receive crypto.")
    if to_asset_config["type"] == "telco" and (not req.destination_phone or len(req.destination_phone) < 10):
        raise HTTPException(400, "Valid phone number is required for Airtime.")

    # 2. Calculate backend rate (Security measure)
    rate = calculate_backend_rate(req.from_asset, req.to_asset)
    to_amount = req.from_amount * rate

    # 3. Check User Balance
    user_wallet = await db["user_wallets"].find_one({"_id": req.user_id})
    if not user_wallet:
        raise HTTPException(404, "User wallet not found.")
        
    current_balance = user_wallet.get("balances", {}).get(req.from_asset, 0.0)
    if current_balance < req.from_amount:
        raise HTTPException(400, f"Insufficient {req.from_asset} balance. You have {current_balance}.")

    # 4. DEDUCT FROM DATABASE IMMEDIATELY (Escrow / Freeze funds)
    await db["user_wallets"].update_one(
        {"_id": req.user_id},
        {"$inc": {f"balances.{req.from_asset}": -req.from_amount}}
    )

    # 5. Create Swap Record in DB
    swap_id = f"SWP-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{req.user_id[:4]}"
    
    await db["swaps"].insert_one({
        "_id": swap_id,
        "user_id": req.user_id,
        "from_asset": req.from_asset,
        "to_asset": req.to_asset,
        "from_amount": req.from_amount,
        "to_amount": to_amount,
        "rate": rate,
        "destination_address": req.destination_address,
        "destination_phone": req.destination_phone,
        "status": "PENDING_SETTLEMENT", 
        "created_at": datetime.utcnow()
    })

    # 6. Send the heavy lifting (API calls) to the background
    background_tasks.add_task(
        settle_swap_in_background, 
        swap_id, 
        {
            "user_id": req.user_id,
            "from_asset": req.from_asset,
            "to_asset": req.to_asset,
            "from_amount": req.from_amount,
            "to_amount": to_amount,
            "destination_address": req.destination_address,
            "destination_phone": req.destination_phone
        }, 
        db
    )

    # 7. Return instantly to React
    return SwapResponse(
        swap_id=swap_id,
        status="PENDING_SETTLEMENT",
        message=f"Deducted {req.from_amount} {req.from_asset}. Settling {to_amount} {req.to_asset} in real-time."
    )