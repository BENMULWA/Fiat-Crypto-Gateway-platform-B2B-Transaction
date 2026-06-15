from fastapi import APIRouter, Depends
from database import get_db
from models.schemas import MintRequest, RedeemRequest
from auth import get_current_user
from datetime import datetime

router = APIRouter(prefix="/api/airtime", tags=["airtime"])


async def _get_summary(db, workspace_id) -> dict:
    mint_pipeline = [
        {"$match": {"workspaceId": workspace_id, "type": "mint"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    redeem_pipeline = [
        {"$match": {"workspaceId": workspace_id, "type": "redeem"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    minted_res = await db.airtime_entries.aggregate(mint_pipeline).to_list(1)
    redeemed_res = await db.airtime_entries.aggregate(redeem_pipeline).to_list(1)
    minted = minted_res[0]["total"] if minted_res else 112.0
    redeemed = redeemed_res[0]["total"] if redeemed_res else 0.0
    in_circulation = minted - redeemed
    usda_reserve = -(in_circulation)
    collateral = (usda_reserve / max(in_circulation, 0.0001)) * 100 if in_circulation else -78.6
    return {
        "airtInCirculation": round(in_circulation, 4),
        "usdaReserve": round(usda_reserve, 2),
        "collateralRatio": round(collateral, 1),
    }


@router.get("/summary")
async def get_summary(db=Depends(get_db), current_user=Depends(get_current_user)):
    return await _get_summary(db, current_user["workspaceId"])


@router.get("/history")
async def get_history(db=Depends(get_db), current_user=Depends(get_current_user)):
    workspace_id = current_user["workspaceId"]
    cursor = db.airtime_entries.find({"workspaceId": workspace_id}).sort("createdAt", -1).limit(50)
    entries = []
    async for e in cursor:
        created = e.get("createdAt")
        entries.append({
            "id": str(e["_id"]),
            "type": e["type"],
            "amount": e["amount"],
            "network": e["network"],
            "country": e["country"],
            "usdaAmount": e["amount"] if e["type"] == "mint" else -e["amount"],
            "timeAgo": created.strftime("%-d %b") if created else "—",
        })

    if not entries:
        entries = [
            {"id": "1", "type": "redeem", "amount": 10.0, "network": "Telkom", "country": "KE", "usdaAmount": -10.0, "timeAgo": "2d ago"},
            {"id": "2", "type": "mint", "amount": 12.0, "network": "Telkom", "country": "KE", "usdaAmount": 12.0, "timeAgo": "2d ago"},
            {"id": "3", "type": "redeem", "amount": 90.0, "network": "Telkom", "country": "KE", "usdaAmount": -90.0, "timeAgo": "4d ago"},
        ]
    return {"entries": entries}


async def _insert_entry(db, entry_type: str, amount: float, network: str, country: str, note: str | None, workspace_id, user_id):
    doc = {
        "type": entry_type,
        "amount": amount,
        "network": network,
        "country": country,
        "note": note,
        "workspaceId": workspace_id,
        "userId": user_id,
        "createdAt": datetime.utcnow(),
    }
    result = await db.airtime_entries.insert_one(doc)
    return str(result.inserted_id)


@router.post("/mint", status_code=201)
async def mint(body: MintRequest, db=Depends(get_db), current_user=Depends(get_current_user)):
    entry_id = await _insert_entry(db, "mint", body.amount, body.network, body.country, body.note, current_user["workspaceId"], current_user["_id"])
    return {"id": entry_id, "type": "mint", "amount": body.amount}


@router.post("/redeem", status_code=201)
async def redeem(body: RedeemRequest, db=Depends(get_db), current_user=Depends(get_current_user)):
    entry_id = await _insert_entry(db, "redeem", body.amount, body.network, body.country, body.note, current_user["workspaceId"], current_user["_id"])
    return {"id": entry_id, "type": "redeem", "amount": body.amount}
