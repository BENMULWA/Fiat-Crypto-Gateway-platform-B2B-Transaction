from fastapi import APIRouter, Depends
from database import get_db

# This prefix matches exactly what React is looking for: /api/ledger
router = APIRouter(prefix="/api/ledger", tags=["General Ledger"])

@router.get("/feed")
async def get_live_ledger_feed(limit: int = 50, search: str = None, db = Depends(get_db)):
    """
    Fetches the most recent transactions from the MongoDB Immutable Ledger.
    Includes server-side searching to find old transactions that fell off the top 50.
    """
    query = {}
    if search:
        # Search across multiple columns case-insensitively
        query = {
            "$or": [
                {"txn_id": {"$regex": search, "$options": "i"}},
                {"from_node": {"$regex": search, "$options": "i"}},
                {"to_node": {"$regex": search, "$options": "i"}},
                {"txn_type": {"$regex": search, "$options": "i"}}
            ]
        }

    # Query MongoDB: Get top transactions matching query, sorted by newest first
    cursor = db["transactions"].find(query).sort("timestamp", -1).limit(limit)
    records = await cursor.to_list(length=limit)
    
    formatted_feed = []
    for r in records:
        # 1. Determine the color of the text based on the transaction type
        type_color = "text-blue-400"
        # Handle both 'txn_type' and 'type' depending on how it was saved
        txn_type = r.get("txn_type", r.get("type", "TRANSFER"))
        
        if txn_type in ["MINT", "SYSTEM_FUND"]:
            type_color = "text-emerald-400"
        elif txn_type in ["LIQUIDATE", "PROCURE"]:
            type_color = "text-orange-400"
        elif txn_type == "CELO_EXIT":
            type_color = "text-purple-400"

        # 2. Format time safely
        timestamp = r.get("timestamp")
        time_str = timestamp.strftime("%H:%M:%S.%f")[:-3] if timestamp else "00:00:00"
        
        # 3. Format value safely
        int_val = r.get('internal_usd_value', 0)
        amount_val = r.get('amount', 0)
        asset_str = r.get('asset', '').replace('_KES', '')

        # 4. Construct the UI-ready object
        formatted_feed.append({
            "time": time_str,
            "id": r.get("txn_id", str(r.get("_id", ""))),
            "from": r.get("from_node", ""),
            "to": r.get("to_node", ""),
            "amount": f"{amount_val:,.2f} {asset_str}",
            "intValue": f"${int_val:,.2f}",
            "type": txn_type,
            "typeColor": type_color
        })
        
    return {"status": "success", "feed": formatted_feed}

# --- Legacy fallback routes to prevent other 404s ---
@router.get("/summary")
async def get_ledger_summary():
    return {"status": "success", "data": {}}

@router.get("/entries")
async def get_ledger_entries(flow: str = None, search: str = None):
    return {"status": "success", "data": []}