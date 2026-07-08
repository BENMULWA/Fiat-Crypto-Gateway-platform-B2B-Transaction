from fastapi import APIRouter
from pydantic import BaseModel
from Brain_Engine.cache import memory_cache

router = APIRouter(prefix="/api/market-maker", tags=["Market Maker API"])

class SpreadUpdate(BaseModel):
    active: bool
    autoPeg: bool
    bid: float
    ask: float

@router.get("/spread")
async def get_spread_config():
    """Fetches the live pricing configuration from the fast-memory cache."""
    return {
        "active": memory_cache.get("spread:usda_kes:active") if memory_cache.get("spread:usda_kes:active") is not None else True,
        "autoPeg": memory_cache.get("spread:usda_kes:auto_peg") if memory_cache.get("spread:usda_kes:auto_peg") is not None else True,
        "bid": memory_cache.get("spread:usda_kes:bid") or 128.00,
        "ask": memory_cache.get("spread:usda_kes:ask") or 132.00,
        "reference": memory_cache.get("rates:binance_usdt_kes") or 130.50
    }

@router.post("/spread")
async def update_spread_config(config: SpreadUpdate):
    """Admin updates the spread. Instantly applied to all retail quotes."""
    memory_cache.set("spread:usda_kes:active", config.active)
    memory_cache.set("spread:usda_kes:auto_peg", config.autoPeg)
    memory_cache.set("spread:usda_kes:bid", config.bid)
    memory_cache.set("spread:usda_kes:ask", config.ask)
    
    return {"status": "success", "message": "Spread parameters updated globally!"}