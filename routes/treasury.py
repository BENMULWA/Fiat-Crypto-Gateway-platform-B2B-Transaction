from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import traceback
from datetime import datetime
import uuid
import requests
import os

from database import get_db
from Brain_Engine.corridor_1_airtime import AirtimeCeloCorridor
from services.safaricom_daraja import DarajaService

router = APIRouter(prefix="/api/treasury", tags=["Treasury"])
daraja = DarajaService()

class CorridorRequest(BaseModel):
    amount_kes: float

@router.post("/corridor/airtime-celo")
async def trigger_airtime_celo_corridor(req: CorridorRequest, db=Depends(get_db)):
    if req.amount_kes <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero.")

    try:
        corridor = AirtimeCeloCorridor(db_collection=db["transactions"])
        result = await corridor.execute_from_kes(deployed_kes=req.amount_kes)
        
        return {
            "status": "success",
            "message": f"Airtel -> Celo Corridor executed. Yielded {result['yield_percent']}%",
            "data": result
        }
        
    except Exception as e:
        traceback.print_exc() 
        print(f"Corridor Execution Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

class HFTExecuteRequest(BaseModel):
    amount: float
    corridor_id: str

@router.post("/corridor/execute-hft")
async def execute_dynamic_hft_corridor(req: HFTExecuteRequest, db=Depends(get_db)):
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero.")

    try:
        from Brain_Engine.state_engine import ImmutableLedger, HFTCorridorFSM, FSMState
        ledger = ImmutableLedger(db_collection=db["transactions"])
        
        config = {}
        if req.corridor_id == "telkom_5x":
            config = {"cycles": 5, "discount": 0.10, "fx_edge": 0.05, "node_procure": "N1", "node_liquidate": "N4"}
        elif req.corridor_id == "airtel_5x":
            config = {"cycles": 5, "discount": 0.06, "fx_edge": 0.00, "node_procure": "N2", "node_liquidate": "N5"}
        else:
            raise HTTPException(status_code=400, detail="Unknown corridor ID")
            
        bot = HFTCorridorFSM(ledger=ledger, starting_capital_usd=req.amount, config=config)
        await bot.boot_system()
        
        while bot.state != FSMState.COMPLETED and bot.state != FSMState.HALTED:
            await bot.tick()
            
        if bot.state == FSMState.HALTED:
            raise Exception("Corridor halted due to internal error or low liquidity.")
            
        return {
            "status": "success",
            "message": f"{config['cycles']}x Rollover Complete via {config['node_procure']}! Exited to Celo.",
            "data": {
                "starting_usd": req.amount,
                "final_usd": bot.current_usd_principal,
                "profit": bot.current_usd_principal - req.amount
            }
        }
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/dashboard")
async def get_treasury_dashboard(db=Depends(get_db)):
    try:
        cursor = db["transactions"].find().sort("timestamp", -1).limit(10)
        records = await cursor.to_list(length=10)
        
        settlements = []
        for r in records:
            amount = r.get("amount", 0)
            asset = r.get("asset", "").replace("_KES", "")
            txn_type = r.get("txn_type", r.get("type", "UNKNOWN"))
            profit_str = f"+ {amount:,.2f} {asset}" if txn_type == "PNL_CAPTURE" else f"0.00 {asset}"
            
            settlements.append({
                "id": str(r.get("_id", uuid.uuid4())),
                "desc": f"Engine Executed {txn_type}",
                "time": r.get("timestamp", datetime.utcnow()).strftime("%H:%M:%S"),
                "status": "COMPLETED",
                "profit": profit_str
            })

        vaults = {
            "N7_USDA": 0.0, "N4_MPESA": 0.0, "N1_TELKOM": 0.0, "N2_AIRTEL": 0.0,
            "N3_SAFARICOM": 0.0, "N8_IMP": 0.0, "N9_XLM": 0.0, "N10_USD": 0.0, "N11_GOLD": 0.0
        }
        
        all_txns_cursor = db["transactions"].find()
        all_txns = await all_txns_cursor.to_list(length=None)
        
        for txn in all_txns:
            amt = txn.get("amount", 0)
            frm = txn.get("from_node")
            to = txn.get("to_node")
            if frm in vaults: vaults[frm] -= amt
            if to in vaults: vaults[to] += amt

        mam_laka_res = daraja.get_merchant_balance()
        fiat_balances = mam_laka_res.get("data", {}) if mam_laka_res.get("status") == "success" else {}
        mam_laka_total = 0.0

        if fiat_balances:
            vaults["N2_AIRTEL"] = fiat_balances.get("artmBalance", 0.0) 
            vaults["N4_MPESA"] = fiat_balances.get("kesBalance", 0.0)   
            vaults["N8_IMP"] = fiat_balances.get("impaBalance", 0.0)    
            mam_laka_total = fiat_balances.get("totalBalance", 0.0)

        return {
            "status": "success",
            "vaults": vaults,
            "settlements": settlements,
            "web2_total_kes": mam_laka_total
        }
    except Exception as e:
        traceback.print_exc()
        return {"status": "success", "vaults": {}, "settlements": []}

@router.post("/reset-sandbox")
async def reset_treasury_sandbox(db=Depends(get_db)):
    await db["transactions"].delete_many({})
    genesis_entries = [
        {"txn_id": "GENESIS-01", "timestamp": datetime.utcnow(), "from_node": "EXTERNAL", "to_node": "N7_USDA", "asset": "USDA", "amount": 50000.00, "internal_usd_value": 50000.00, "txn_type": "SYSTEM_FUND", "cycle": 0},
        {"txn_id": "GENESIS-02", "timestamp": datetime.utcnow(), "from_node": "EXTERNAL", "to_node": "N4_MPESA", "asset": "KES", "amount": 6500000.00, "internal_usd_value": 50000.00, "txn_type": "SYSTEM_FUND", "cycle": 0}
    ]
    await db["transactions"].insert_many(genesis_entries)
    try:
        from Brain_Engine.cache import memory_cache
        memory_cache.set("system:kill_switch", False)
    except:
        pass
    return {"status": "success", "message": "Sandbox reset to Genesis. Kill switch lifted."}

@router.post("/kill-switch")
async def toggle_kill_switch(req: dict):
    try:
        from Brain_Engine.cache import memory_cache
        memory_cache.set("system:kill_switch", req.get("active", False))
    except:
        pass
    return {"status": "success"}


@router.get("/mpesa/balances")
async def get_mpesa_balances():
    """Mock endpoint for M-Pesa B2C and C2B float balances."""
    return {
        "status": "success",
        "payouts_kes": 6500000.0,
        "collections_kes": 1250000.0
    }

# ======================================================================
# 🟢 CORPORATE REVENUE ENDPOINTS
# ======================================================================
@router.get("/revenue")
async def get_corporate_revenue(db=Depends(get_db)):
    """Fetches the accumulated profit separated from user liquidity."""
    revenue = await db["company_revenue"].find_one({"_id": "corporate_treasury"})
    if not revenue:
        return {
            "status": "success", 
            "balances": {
                        "KES": 0.0, "USD": 0.0,
                        "USDC": 0.0, "USDA": 0.0, 
                        "AIRT": 0.0
                        }
                }
    
    balances = {k: v for k, v in revenue.items() if k != "_id"}
    return {"status": "success", "balances": balances}

class RevenueWithdrawal(BaseModel):
    asset: str
    amount: float
    destination: str

#===================================================================
# Withdraw endpoint 
#===================================================================

@router.post("/revenue/withdraw")
async def withdraw_corporate_revenue(payload: RevenueWithdrawal, db=Depends(get_db)):
    """Allows the Admin to cash out accumulated company profits."""
    # 1. Check Revenue Balance
    revenue = await db["company_revenue"].find_one({"_id": "corporate_treasury"})
    current_balance = float(revenue.get(payload.asset, 0.0)) if revenue else 0.0
    
    if payload.amount > current_balance:
        raise HTTPException(status_code=400, detail=f"Insufficient {payload.asset} in Corporate Revenue Account.")

    # 2. Safely Lock Funds in Database
    await db["company_revenue"].update_one(
        {"_id": "corporate_treasury"},
        {"$inc": {payload.asset: -payload.amount}}
    )

    # 3. Execute the Admin Withdrawal (M-Pesa B2C for KES)
    if payload.asset == "KES":
        try:
            token = daraja.get_access_token()
            payout_url = f"{daraja.base_url}/api/v1/mobile/transfer"
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            
            b2c_payload = {
                "impalaMerchantId": "meshex_sandbox",
                "currency": "KES",
                "amount": int(payload.amount),
                "recipientPhone": payload.destination,
                "mobileMoneySP": "M-Pesa",
                "externalId": f"REV_{uuid.uuid4().hex[:6].upper()}",
                "callbackUrl": "https://mamlaka-test.ngrok.app/api/ramp/b2c/result"
            }
            
            res = requests.post(payout_url, json=b2c_payload, headers=headers, timeout=15)
            if res.status_code not in [200, 201]:
                raise Exception(f"M-Pesa API rejected transfer: {res.text}")
                
            print(f"💰 Corporate Revenue Withdrawn! Sent {payload.amount} KES to {payload.destination}.")
        except Exception as e:
            # Safely refund the revenue wallet if the Daraja API fails
            await db["company_revenue"].update_one({"_id": "corporate_treasury"}, {"$inc": {payload.asset: payload.amount}})
            raise HTTPException(status_code=500, detail=str(e))
            
    return {"status": "success", "message": f"Successfully withdrew {payload.amount} {payload.asset} to {payload.destination}."}

class SimSwapReq(BaseModel):
    user_id: str
    from_asset: str
    to_asset: str
    amount: float

@router.post("/simulate-swap")
async def simulate_swap(req: SimSwapReq, db=Depends(get_db)):
    from routes.ramp import execute_internal_swap
    return await execute_internal_swap(req, db)
