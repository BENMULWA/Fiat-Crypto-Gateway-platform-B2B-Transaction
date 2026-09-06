from fastapi import APIRouter, Depends
from database import get_db
from models.schemas import DiscountRateCreate
from auth import get_current_user
from datetime import datetime

router = APIRouter(prefix="/api/rates", tags=["rates"])


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    doc.pop("workspaceId", None)
    doc.pop("userId", None)
    doc.pop("createdAt", None)
    return doc


@router.get("/discount")
async def get_discount_rates(db=Depends(get_db), current_user=Depends(get_current_user)):
    workspace_id = current_user["workspaceId"]
    cursor = db.discount_rates.find({"workspaceId": workspace_id})
    rates = [_serialize(r) async for r in cursor]
    if not rates:
        rates = [{"id": "1", "network": "Telkom", "country": "KE", "product": "data", "rate": 10.0}]
    return {"rates": rates}


@router.post("/discount", status_code=201)
async def add_discount_rate(body: DiscountRateCreate, db=Depends(get_db), current_user=Depends(get_current_user)):
    doc = {
        "network": body.network,
        "country": body.country,
        "product": body.product.lower(),
        "rate": body.rate,
        "workspaceId": current_user["workspaceId"],
        "userId": current_user["_id"],
        "createdAt": datetime.utcnow(),
    }
    result = await db.discount_rates.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _serialize(doc)


@router.get("/inventory")
async def get_inventory(db=Depends(get_db), current_user=Depends(get_current_user)):
    workspace_id = current_user["workspaceId"]
    cursor = db.inventory.find({"workspaceId": workspace_id})
    items = []
    async for item in cursor:
        items.append({
            "id": str(item["_id"]),
            "network": item.get("network", ""),
            "country": item.get("country", ""),
            "product": item.get("product", ""),
            "stock": item.get("stock", 0),
            "margin": item.get("margin", 0.0),
        })
    return {"items": items}
