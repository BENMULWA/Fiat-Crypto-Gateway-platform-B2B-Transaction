from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from datetime import datetime
import uuid

from database import get_db
from Brain_Engine.cache import memory_cache

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
    current_user = {"_id": "usr_retail_001"}
    trade_id = f"TRADE_{uuid.uuid4().hex[:8].upper()}"
    
    # 1. SECURITY: Check if Trading is Halted by Dealing Desk
    is_active = memory_cache.get("spread:usda_kes:active")
    if is_active is False:
        raise HTTPException(status_code=503, detail="Trading for this route is currently halted by the Dealing Desk.")

    # 2. FETCH LIVE RATES FROM CACHE
    market_rate = memory_cache.get("rates:binance_usdt_kes") or 130.50
    bid_rate = memory_cache.get("spread:usda_kes:bid") or 128.00
    ask_rate = memory_cache.get("spread:usda_kes:ask") or 132.00

    receive = 0.0

    if body.direction == "swap":
        # ========================================================
        # 🚀 TRUE DOUBLE-ENTRY INTERNAL MARKET TRADING
        # ========================================================
        ledger_entries = []
        
        if body.from_asset == "USDA" and body.to_asset == "KES":
            # User SELLS USDA for KES (Mamlaka buys)
            executed_rate = bid_rate
            receive = round(body.amount * executed_rate, 2)
            profit_kes = round((market_rate - executed_rate) * body.amount, 2)
            
            # Entry 1: User gives USDA -> N7_USDA (Master Wallet goes UP)
            ledger_entries.append({
                "txn_id": trade_id, "timestamp": datetime.utcnow(),
                "from_node": "RETAIL_SPOKE", "to_node": "N7_USDA",
                "asset": "USDA", "amount": body.amount, 
                "internal_usd_value": body.amount, "txn_type": "RETAIL_SWAP", "cycle": 0
            })
            # Entry 2: N4_MPESA gives KES -> User (Paybill goes DOWN)
            ledger_entries.append({
                "txn_id": trade_id, "timestamp": datetime.utcnow(),
                "from_node": "N4_MPESA", "to_node": "RETAIL_SPOKE",
                "asset": "KES", "amount": receive, 
                "internal_usd_value": body.amount, "txn_type": "RETAIL_SWAP", "cycle": 0
            })

        elif body.from_asset == "KES" and body.to_asset == "USDA":
            # User BUYS USDA with KES (Mamlaka sells)
            executed_rate = ask_rate
            receive = round(body.amount / executed_rate, 4)
            profit_kes = round(body.amount - (receive * market_rate), 2)
            
            # Entry 1: User gives KES -> N4_MPESA (Paybill goes UP)
            ledger_entries.append({
                "txn_id": trade_id, "timestamp": datetime.utcnow(),
                "from_node": "RETAIL_SPOKE", "to_node": "N4_MPESA",
                "asset": "KES", "amount": body.amount, 
                "internal_usd_value": receive, "txn_type": "RETAIL_SWAP", "cycle": 0
            })
            # Entry 2: N7_USDA gives USDA -> User (Master Wallet goes DOWN)
            ledger_entries.append({
                "txn_id": trade_id, "timestamp": datetime.utcnow(),
                "from_node": "N7_USDA", "to_node": "RETAIL_SPOKE",
                "asset": "USDA", "amount": receive, 
                "internal_usd_value": receive, "txn_type": "RETAIL_SWAP", "cycle": 0
            })
        else:
            # Fallback for other assets (UGX, etc)
            receive = body.amount * body.rate

        # 3. INJECT INTO IMMUTABLE LEDGER
        if ledger_entries:
            await db["transactions"].insert_many(ledger_entries)

        # 4. CAPTURE REALIZED P&L TO SETTLEMENT TAPE
        if 'profit_kes' in locals() and profit_kes > 0:
            await db["transactions"].insert_one({
                "txn_id": f"PNL_{trade_id}", "timestamp": datetime.utcnow(),
                "from_node": "SPREAD_ENGINE", "to_node": "TREASURY_PNL",
                "asset": "KES", "amount": profit_kes, 
                "internal_usd_value": profit_kes / market_rate, "txn_type": "PNL_CAPTURE", "cycle": 0
            })

        # 5. USER UI RECEIPT
        doc = {
            "_id": trade_id, "direction": body.direction, "channel": body.channel,
            "fromAsset": body.from_asset, "toAsset": body.to_asset,
            "fromAmount": body.amount, "toAmount": receive,
            "status": "completed", "userId": current_user["_id"],
            "date": datetime.utcnow().strftime("%b %d, %Y"), "timeAgo": "Just now"
        }
        await db["ramp_entries"].insert_one(doc)

        return {"id": trade_id, "receive": receive, "status": "completed", "message": "Double-Entry Swap Executed & Profit Captured!"}

    return {"id": trade_id, "receive": receive, "status": "processing"}

# ========================================================
# NEW: STK PUSH WEBHOOK (FIAT -> INTERNAL AIRTIME VALUE)
# ========================================================
@router.post("/b2c/result") # This URL must match your Mam-laka CallbackUrl!
async def mamlaka_stk_callback(payload: dict, db=Depends(get_db)):
    """
    Catches the STK Push success receipt from Mam-laka.
    Converts deposited KES directly into internal Airtime Credits (AIRT).
    """
    # 1. Parse the Mam-laka payload
    status = payload.get("status")
    amount = float(payload.get("amount", 0))
    # You would pass the user_id in the 'externalId' when initiating the STK push
    # e.g., "usr_retail_001_8f7d6c"
    external_id = payload.get("externalId", "") 
    
    if status == "success" and "usr_" in external_id:
        # Extract the user ID from the external_id string
        # user_id = external_id.split("_")[1] # Example extraction
        user_id = "test_user_123" 
        
        # 2. ATOMIC UPDATE: Give the user Internal Airtime (AIRT) equivalent to their deposit
        await db["user_wallets"].update_one(
            {"_id": user_id},
            {"$inc": {"balances.AIRT": amount}}, 
            upsert=True 
        )
        print(f"✅ STK Push Success: Credited {amount} AIRT to user {user_id}")

        # 3. 🔄 THE AUTO-SWEEP 🔄
        # We instantly command Mam-laka to convert this KES into Airtime Inventory
        from services.safaricom_daraja import DarajaService
        mam_laka = DarajaService()
        
        # We run this asynchronously so it doesn't block the webhook response
        import asyncio
        asyncio.create_task(asyncio.to_thread(mam_laka.auto_sweep_kes_to_artm, int(amount)))
        
    return {"status": "acknowledged"}

@router.get("/history")
async def get_ramp_history(db=Depends(get_db)):
    current_user = {"_id": "usr_retail_001"}
    cursor = db["ramp_entries"].find({"userId": current_user["_id"]}).sort("_id", -1).limit(20)
    entries = await cursor.to_list(length=20)
    
    formatted_entries = []
    for e in entries:
        formatted_entries.append({
            "id": str(e["_id"]), "direction": e.get("direction", "swap"),
            "channel": e.get("channel", "Internal Ledger"), "fromAsset": e.get("fromAsset", "KES"),
            "toAsset": e.get("toAsset", "USDA"), "fromAmount": e.get("fromAmount", 0),
            "toAmount": e.get("toAmount", 0), "status": e.get("status", "completed"),
            "date": e.get("date", "Today"), "timeAgo": e.get("timeAgo", "Recently")
        })
    return {"status": "success", "entries": formatted_entries}