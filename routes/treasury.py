from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import datetime
from database import get_system_vaults_col, get_user_wallets_col, get_settlement_logs_col

router = APIRouter(prefix="/api/treasury", tags=["Treasury & Dealing Desk"])

class SwapSimulation(BaseModel):
    user_id: str
    from_asset: str
    to_asset: str
    amount: float

@router.get("/dashboard")
async def get_treasury_dashboard():
    """Fetches all system balances and recent settlements for the Market Maker page."""
    vaults_col = get_system_vaults_col()
    logs_col = get_settlement_logs_col()

    # 1. Fetch or Auto-Seed the Treasury Vaults
    vaults = await vaults_col.find_one({"_id": "master_treasury"})
    if not vaults:
        vaults = {
            "_id": "master_treasury",
            "N1_TELKOM": 320000.00,
            "N2_AIRTEL": 4500000.00,
            "N3_SAFARICOM": 10000000.00,
            "N4_MPESA": 6500000.00,
            "N5_AIRTEL_MONEY": 1200000.00,
            "N6_TKASH": 850000.00,
            "N7_USDA": 50000.00,
            "N8_IMP": 30000.00,
            "N9_XLM": 150000.00,
            "N10_USD": 25000.00
        }
        await vaults_col.insert_one(vaults)

    # 2. Fetch the latest 5 settlement logs
    cursor = logs_col.find().sort("timestamp", -1).limit(6)
    raw_logs = await cursor.to_list(length=5)
    
    settlements = []
    for log in raw_logs:
        settlements.append({
            "id": str(log["_id"]),
            "desc": log["desc"],
            "time": log["time_str"],
            "status": log["status"],
            "profit": log["profit_str"]
        })

    return {
        "status": "success",
        "vaults": vaults,
        "settlements": settlements
    }

@router.post("/simulate-swap")
async def execute_simulated_trade(req: SwapSimulation):
    """Dynamic End-to-End Simulation handling both Off-Ramp and On-Ramp."""
    vaults_col = get_system_vaults_col()
    users_col = get_user_wallets_col()
    logs_col = get_settlement_logs_col()

    # Market Maker Configuration (Mocked for simulation)
    global_market_rate = 130.50 # True global value
    mamlaka_bid_rate = 128.00   # What we BUY USDA from user for
    mamlaka_ask_rate = 132.00   # What we SELL USDA to user for
    
    # 1. Ensure testing user exists with plenty of fake funds
    user = await users_col.find_one({"_id": req.user_id})
    if not user:
        user = {"_id": req.user_id, "balances":
                 {"USDA": 10000.00, "KES": 500000.00,
                  "AIRT": 50000.00, "IMP": 50000.00,
                  "XLM" : 100000.00, "USD" :  5000.00
                  }}
        await users_col.insert_one(user)

        # Function to quickly log the receipt
    async def log_receipt(desc, profit_str):
        await logs_col.insert_one({
            "desc": desc,
            "time_str": "Just now",
            "timestamp": datetime.now(),
            "status": "COMPLETED",
            "profit_str": profit_str
        })    
 # --- SCENARIO A: USDA <-> KES (Remittance Spread) ---
    if req.from_asset == "USDA" and req.to_asset == "KES":
        payout = req.amount * 128.00 # Bid
        profit = (130.50 - 128.00) * req.amount
        await users_col.update_one({"_id": req.user_id}, {"$inc": {"balances.USDA": -req.amount, "balances.KES": payout}})
        await vaults_col.update_one({"_id": "master_treasury"}, {"$inc": {"N7_USDA": req.amount, "N4_MPESA": -payout}})
        await log_receipt(f"{req.amount:,.2f} USDA → {payout:,.2f} KES", f"+ {profit:,.2f} KES")
        return {"status": "success"}
        
    elif req.from_asset == "KES" and req.to_asset == "USDA":
        payout = req.amount / 132.00 # Ask
        profit = req.amount - (payout * 130.50)
        await users_col.update_one({"_id": req.user_id}, {"$inc": {"balances.KES": -req.amount, "balances.USDA": payout}})
        await vaults_col.update_one({"_id": "master_treasury"}, {"$inc": {"N4_MPESA": req.amount, "N7_USDA": -payout}})
        await log_receipt(f"{req.amount:,.2f} KES → {payout:,.2f} USDA", f"+ {profit:,.2f} KES")
        return {"status": "success"}

    # --- SCENARIO B: AIRT <-> IMP (Synthetic Peg Minting) ---
    elif req.from_asset == "AIRT" and req.to_asset == "IMP":
        payout = req.amount * 1.00 # 1:1 Peg
        profit = 0.00 # No spread on minting
        await users_col.update_one({"_id": req.user_id}, {"$inc": {"balances.AIRT": -req.amount, "balances.IMP": payout}})
        # Treasury gains Safaricom Airtime (Collateral), gives away IMP
        await vaults_col.update_one({"_id": "master_treasury"}, {"$inc": {"N3_SAFARICOM": req.amount, "N8_IMP": -payout}})
        await log_receipt(f"Minted {payout:,.2f} IMP", f"0.00 KES (Peg)")
        return {"status": "success"}
        
    elif req.from_asset == "IMP" and req.to_asset == "AIRT":
        payout = req.amount * 1.00 # 1:1 Peg
        profit = 0.00
        await users_col.update_one({"_id": req.user_id}, {"$inc": {"balances.IMP": -req.amount, "balances.AIRT": payout}})
        # Treasury burns IMP, releases Safaricom Airtime
        await vaults_col.update_one({"_id": "master_treasury"}, {"$inc": {"N8_IMP": req.amount, "N3_SAFARICOM": -payout}})
        await log_receipt(f"Burned {req.amount:,.2f} IMP", f"0.00 KES (Peg)")
        return {"status": "success"}

    # --- SCENARIO C: XLM <-> USD (Global PSP Routing) ---
    elif req.from_asset == "XLM" and req.to_asset == "USD":
        payout = req.amount * 0.098 # Bid ($0.098 per XLM)
        profit = (0.10 - 0.098) * req.amount # True market is $0.10
        await users_col.update_one({"_id": req.user_id}, {"$inc": {"balances.XLM": -req.amount, "balances.USD": payout}})
        await vaults_col.update_one({"_id": "master_treasury"}, {"$inc": {"N9_XLM": req.amount, "N10_USD": -payout}})
        await log_receipt(f"{req.amount:,.2f} XLM → {payout:,.2f} USD", f"+ ${profit:,.2f} USD")
        return {"status": "success"}

    elif req.from_asset == "USD" and req.to_asset == "XLM":
        payout = req.amount / 0.102 # Ask ($0.102 per XLM)
        profit = req.amount - (payout * 0.10)
        await users_col.update_one({"_id": req.user_id}, {"$inc": {"balances.USD": -req.amount, "balances.XLM": payout}})
        await vaults_col.update_one({"_id": "master_treasury"}, {"$inc": {"N10_USD": req.amount, "N9_XLM": -payout}})
        await log_receipt(f"{req.amount:,.2f} USD → {payout:,.2f} XLM", f"+ ${profit:,.2f} USD")
        return {"status": "success"}

    raise HTTPException(status_code=400, detail=f"Pair {req.from_asset} to {req.to_asset} not supported.")