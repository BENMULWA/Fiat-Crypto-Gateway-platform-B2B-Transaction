from fastapi import APIRouter, Depends
from database import get_db
from models.schemas import OrderCreate
from auth import get_current_user
from datetime import datetime

router = APIRouter(prefix="/api/trade", tags=["trade"])

MOCK_ORDERBOOK = {
    "bids": [{"price": 1.0, "amount": 200.0}],
    "asks": [],
}


@router.get("/orderbook")
async def get_orderbook(db=Depends(get_db), current_user=Depends(get_current_user)):
    workspace_id = current_user["workspaceId"]
    bids = await db.orders.find({"workspaceId": workspace_id, "side": "buy", "status": "open"}).to_list(50)
    asks = await db.orders.find({"workspaceId": workspace_id, "side": "sell", "status": "open"}).to_list(50)
    if not bids and not asks:
        return MOCK_ORDERBOOK
    return {
        "bids": [{"price": o["price"], "amount": o["amount"]} for o in bids],
        "asks": [{"price": o["price"], "amount": o["amount"]} for o in asks],
    }


@router.get("/history")
async def get_history(db=Depends(get_db), current_user=Depends(get_current_user)):
    workspace_id = current_user["workspaceId"]
    cursor = db.orders.find({"workspaceId": workspace_id, "status": {"$in": ["filled", "cancelled"]}}).sort("createdAt", -1).limit(50)
    trades = []
    async for o in cursor:
        trades.append({
            "id": str(o["_id"]),
            "side": o["side"],
            "price": o["price"],
            "amount": o["amount"],
            "status": o["status"],
            "createdAt": o.get("createdAt", "").isoformat() if hasattr(o.get("createdAt"), "isoformat") else str(o.get("createdAt", "")),
        })
    return {"trades": trades}


@router.post("/orders", status_code=201)
async def place_order(body: OrderCreate, db=Depends(get_db), current_user=Depends(get_current_user)):
    doc = {
        "side": body.side,
        "price": body.price,
        "amount": body.amount,
        "total": round(body.price * body.amount, 4),
        "status": "open",
        "workspaceId": current_user["workspaceId"],
        "userId": current_user["_id"],
        "createdAt": datetime.utcnow(),
    }
    result = await db.orders.insert_one(doc)
    return {"id": str(result.inserted_id), "status": "open"}
