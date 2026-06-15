from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId
from database import get_db
from models.schemas import QuoteCreate
from auth import get_current_user

router = APIRouter(prefix="/api/market-maker", tags=["market-maker"])


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    doc["workspaceId"] = str(doc.get("workspaceId", ""))
    return doc


def _calc_prices(pair: str, bank_spread: float, bank_ref: str) -> dict:
    # Simulated bank rates
    base_rates = {"USD/KES": 129.0, "USD/UGX": 3750.0, "USD/TZS": 2580.0, "USDA/KES": 129.0}
    mid = base_rates.get(pair, 1.0)
    half = bank_spread / 2
    bank_buy = round(mid - half, 4)
    bank_sell = round(mid + half, 4)
    markup = bank_spread * 0.5
    you_buy = round(mid - half - markup, 4)
    you_sell = round(mid + half + markup, 4)
    your_spread = round(you_sell - you_buy, 4)
    return {
        "bankRef": bank_ref,
        "bankSpread": bank_spread,
        "bankBuy": bank_buy,
        "bankSell": bank_sell,
        "youBuy": you_buy,
        "youSell": you_sell,
        "yourSpread": your_spread,
    }


@router.get("/quotes")
async def list_quotes(db=Depends(get_db), current_user=Depends(get_current_user)):
    workspace_id = current_user["workspaceId"]
    cursor = db.quotes.find({"workspaceId": workspace_id})
    quotes = [_serialize(q) async for q in cursor]
    return {"quotes": quotes}


@router.post("/quotes", status_code=201)
async def create_quote(body: QuoteCreate, db=Depends(get_db), current_user=Depends(get_current_user)):
    prices = _calc_prices(body.pair, body.bankSpread, body.bankRef)
    doc = {
        "pair": body.pair,
        "isLive": False,
        "workspaceId": current_user["workspaceId"],
        **prices,
    }
    result = await db.quotes.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _serialize(doc)


@router.delete("/quotes/{quote_id}")
async def delete_quote(quote_id: str, db=Depends(get_db), current_user=Depends(get_current_user)):
    result = await db.quotes.delete_one({"_id": ObjectId(quote_id), "workspaceId": current_user["workspaceId"]})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Quote not found.")
    return {"ok": True}


@router.patch("/quotes/{quote_id}/toggle")
async def toggle_quote(quote_id: str, db=Depends(get_db), current_user=Depends(get_current_user)):
    quote = await db.quotes.find_one({"_id": ObjectId(quote_id), "workspaceId": current_user["workspaceId"]})
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found.")
    new_state = not quote["isLive"]
    await db.quotes.update_one({"_id": ObjectId(quote_id)}, {"$set": {"isLive": new_state}})
    return {"isLive": new_state}


@router.post("/quotes/{quote_id}/book")
async def book_deal(quote_id: str, db=Depends(get_db), current_user=Depends(get_current_user)):
    quote = await db.quotes.find_one({"_id": ObjectId(quote_id)})
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found.")
    deal_doc = {
        "quoteId": ObjectId(quote_id),
        "pair": quote["pair"],
        "youBuy": quote["youBuy"],
        "youSell": quote["youSell"],
        "workspaceId": current_user["workspaceId"],
        "bookedBy": current_user["_id"],
    }
    result = await db.deals.insert_one(deal_doc)
    return {"dealId": str(result.inserted_id), "ok": True}
