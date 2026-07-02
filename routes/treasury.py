from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from datetime import datetime
import uuid

from database import get_db 

router = APIRouter(prefix="/api/treasury", tags=["Treasury & Market Maker"])

# --- 1. PYDANTIC MODELS ---
class SimulateSwapRequest(BaseModel):
    user_id: str
    from_asset: str
    to_asset: str
    amount: float
    simulate_fail: bool = False
    fail_reason: str = None

# --- 2. CONFIGURATION & MAPPING ---
# The massive starting capital injected into the system on Day 1
STARTING_CAPITAL = {
    "N1_TELKOM": 13050000.0, "N2_AIRTEL": 13050000.0, "N3_SAFARICOM": 13050000.0, 
    "N4_MPESA": 30177200.0, "N5_AIRTEL_MONEY": 1000000.0, "N6_TKASH": 1000000.0,     
    "N7_USDA": 170100.0, "N8_IMP": 100000.0, "N9_XLM": 1000000.0,       
    "N10_USD": 100000.0, "N11_GOLD": 41.66, "N12_YESHARA": 95238.0     
}

ASSET_TO_NODE = {
    "USDA": "N7", "KES": "N4", "AIRT": "N3", "IMP": "N8",
    "XLM": "N9", "USD": "N10", "GOLD": "N11", "YESHARA": "N12"
}

MAMLAKA_RATES = {
    "USDA_KES": 128.00, "KES_USDA": 1/132.00, "AIRT_IMP": 1.00, 
    "IMP_AIRT": 1.00, "XLM_USD": 0.098, "USD_XLM": 1 / 0.102
}
TRUE_MARKET_RATES = { "USDA_KES": 130.50, "XLM_USD": 0.100 }

def map_node_to_vault(node: str, asset: str) -> str:
    """Translates raw Ledger node IDs to UI Vault Keys."""
    if not node: return None
    node = str(node).upper()
    
    mapping = {
        "N1": "N1_TELKOM", "N2": "N2_AIRTEL", "N3": "N3_SAFARICOM",
        "N4": "N4_MPESA", "N5": "N5_AIRTEL_MONEY", "N6": "N6_TKASH",
        "N7": "N7_USDA", "N8": "N8_IMP", "N9": "N9_XLM",
        "N10": "N10_USD", "N11": "N11_GOLD", "N12": "N12_YESHARA"
    }
    if node in mapping:
        return mapping[node]
        
    # Handle Retail Bridge dynamic mapping
    if node in ["TREASURY_HUB", "MARKET"]:
        asset = str(asset).upper()
        if "KES" in asset and "AIRTIME" not in asset: return "N4_MPESA"
        if "AIRTIME" in asset: return "N3_SAFARICOM"
        if "USDA" in asset: return "N7_USDA"
        if "USD" in asset and "USDA" not in asset: return "N10_USD"
        if "IMP" in asset: return "N8_IMP"
        if "XLM" in asset: return "N9_XLM"
        
    return None

# --- 3. CORE ENDPOINTS ---

@router.get("/dashboard")
async def get_treasury_dashboard(db = Depends(get_db)):
    """
    Dynamically aggregates the EXACT live balances based on all trades in the MongoDB Execution Tape.
    """
    vaults = STARTING_CAPITAL.copy()
    
    # A) Aggregate all Credits (Incoming money to nodes)
    credits_cursor = db["transactions"].aggregate([
        {"$group": {"_id": {"node": "$to_node", "asset": "$asset"}, "total": {"$sum": "$amount"}}}
    ])
    credits = await credits_cursor.to_list(length=None)
    
    for c in credits:
        v_key = map_node_to_vault(c["_id"].get("node", ""), c["_id"].get("asset", ""))
        if v_key and v_key in vaults:
            vaults[v_key] += c["total"]
            
    # B) Aggregate all Debits (Outgoing money from nodes)
    debits_cursor = db["transactions"].aggregate([
        {"$group": {"_id": {"node": "$from_node", "asset": "$asset"}, "total": {"$sum": "$amount"}}}
    ])
    debits = await debits_cursor.to_list(length=None)
    
    for d in debits:
        v_key = map_node_to_vault(d["_id"].get("node", ""), d["_id"].get("asset", ""))
        if v_key and v_key in vaults:
            vaults[v_key] -= d["total"]

    # C) Fetch recent settlements (P&L Ledger)
    raw_logs = await db["settlement_logs"].find().sort("timestamp", -1).limit(10).to_list(length=10)
    settlements = [{
        "id": str(log.get("_id", uuid.uuid4())), "desc": log.get("desc", ""),
        "time": log.get("time_str", ""), "status": log.get("status", "COMPLETED"), 
        "profit": log.get("profit_str", "0.00")
    } for log in raw_logs]

    return {
        "status": "success",
        "vaults": vaults,
        "settlements": settlements
    }


@router.post("/simulate-swap")
async def simulate_treasury_swap(req: SimulateSwapRequest, db = Depends(get_db)):
    """
    Executes a swap and injects the double-entry accounting directly into the HFT Transactions ledger!
    """
    now = datetime.utcnow()
    tx_id = f"TXN-{uuid.uuid4().hex[:8].upper()}"

    from_base = req.from_asset.split('-')[0]
    to_base = req.to_asset.split('-')[0]
    pair_key = f"{from_base}_{to_base}"

    rate = MAMLAKA_RATES.get(pair_key, 1.00) 
    receive_amount = req.amount * rate

    from_node_id = ASSET_TO_NODE.get(from_base, "MARKET")
    to_node_id = ASSET_TO_NODE.get(to_base, "MARKET")

    # 1. INCOMING ENTRY: User sends asset to our Vault
    await db["transactions"].insert_one({
        "txn_id": f"{tx_id}-IN",
        "timestamp": now,
        "from_node": "EXTERNAL",
        "to_node": from_node_id,
        "asset": from_base,
        "amount": req.amount,
        "internal_usd_value": req.amount / 130.5 if "KES" in from_base else req.amount,
        "txn_type": "SIM_DEPOSIT",
        "cycle": 0
    })

    # 2. OUTGOING ENTRY: We send the converted asset to the User
    await db["transactions"].insert_one({
        "txn_id": f"{tx_id}-OUT",
        "timestamp": now,
        "from_node": to_node_id,
        "to_node": "EXTERNAL",
        "asset": to_base,
        "amount": receive_amount,
        "internal_usd_value": receive_amount / 130.5 if "KES" in to_base else receive_amount,
        "txn_type": "SIM_PAYOUT",
        "cycle": 0
    })

    # 3. Calculate Profit & Write to Settlement Log
    profit_str = "0.00"
    if pair_key == "USDA_KES":
        profit_str = f"+ {(TRUE_MARKET_RATES['USDA_KES'] - rate) * req.amount:,.2f} KES"
    elif pair_key == "KES_USDA":
        profit_str = f"+ {(1/rate - TRUE_MARKET_RATES['USDA_KES']) * receive_amount:,.2f} KES"

    await db["settlement_logs"].insert_one({
        "_id": tx_id,
        "desc": f"{req.amount:,.2f} {from_base} → {receive_amount:,.2f} {to_base}",
        "time_str": now.strftime("%I:%M:%S %p"),
        "timestamp": now, "status": "COMPLETED", "profit_str": profit_str
    })

    return {"status": "success"}


@router.post("/reset-sandbox")
async def reset_treasury_sandbox(db = Depends(get_db)):
    """Wipes the database cleanly so you can restart the simulation."""
    await db["transactions"].delete_many({}) 
    await db["settlement_logs"].delete_many({}) 
    return {"status": "success", "message": "Sandbox reset to Genesis balances."}