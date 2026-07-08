from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import traceback
from datetime import datetime
import uuid

from database import get_db
from Brain_Engine.corridor_1_airtime import AirtimeCeloCorridor
from services.safaricom_daraja import DarajaService

router = APIRouter(prefix="/api/treasury", tags=["Treasury"])
mam_laka = DarajaService()

# --- Dashboard APIs ---
@router.get("/dashboard")
async def get_treasury_dashboard(db=Depends(get_db)):
    """Serves the Main Treasury Dashboard with live vaults and the settlements tape."""
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

        # Calculate Vaults Dynamically based on Transaction Ledger
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

        return {
            "status": "success",
            "vaults": vaults,
            "settlements": settlements
        }
    except Exception as e:
        traceback.print_exc()
        return {"status": "success", "vaults": {}, "settlements": []}

@router.post("/reset-sandbox")
async def reset_treasury_sandbox(db=Depends(get_db)):
    """Wipes the ledger and injects Genesis Capital so the bot can trade."""
    await db["transactions"].delete_many({})
    
    genesis_entries = [
        {"txn_id": "GENESIS-01", "timestamp": datetime.utcnow(), "from_node": "EXTERNAL", "to_node": "N7_USDA", "asset": "USDA", "amount": 50000.00, "internal_usd_value": 50000.00, "txn_type": "SYSTEM_FUND", "cycle": 0},
        {"txn_id": "GENESIS-02", "timestamp": datetime.utcnow(), "from_node": "EXTERNAL", "to_node": "N4_MPESA", "asset": "KES", "amount": 6500000.00, "internal_usd_value": 50000.00, "txn_type": "SYSTEM_FUND", "cycle": 0}
    ]
    await db["transactions"].insert_many(genesis_entries)
    
    from services.cache import memory_cache
    memory_cache.set("system:kill_switch", False)
    
    return {"status": "success", "message": "Sandbox reset to Genesis. Kill switch lifted."}

@router.post("/kill-switch")
async def toggle_kill_switch(req: dict):
    from services.cache import memory_cache
    memory_cache.set("system:kill_switch", req.get("active", False))
    return {"status": "success"}

class SimSwapReq(BaseModel):
    user_id: str
    from_asset: str
    to_asset: str
    amount: float

@router.post("/simulate-swap")
async def simulate_swap(req: SimSwapReq, db=Depends(get_db)):
    from routes.ramp import execute_internal_swap
    return await execute_internal_swap(req, db)

class CorridorRequest(BaseModel):
    amount_kes: float

@router.post("/corridor/airtime-celo")
async def trigger_airtime_celo_corridor(req: CorridorRequest, db=Depends(get_db)):
    if req.amount_kes <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero.")

    try:
        corridor = AirtimeCeloCorridor(db_collection=db["transactions"])
        # Pass the KES amount directly to our updated function
        result = await corridor.execute_from_kes(deployed_kes=req.amount_kes)
        
        return {
            "status": "success",
            "message": f"Airtel -> Celo Corridor executed. Yielded {result['yield_percent']}%",
            "data": result
        }
        
    except Exception as e:
        traceback.print_exc() 
        print(f"Corridor Execution Error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Backend Error: {str(e)}")