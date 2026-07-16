from fastapi import APIRouter
from pydantic import BaseModel
from Brain_Engine.cache import memory_cache

from Brain_Engine.Discovery_Engine import IMMDiscoveryEngine 


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

@router.get("/opportunities")
async def get_dynamic_opportunities():
    """
    Returns the Ranked Opportunities calculated LIVE by the IMM Discovery Engine.
    This feeds the React frontend so the math is server-authoritative.
    """

    
    engine = IMMDiscoveryEngine()
    baseline = engine.baseline_rate_kes_usd
    
    # 1. Calculate Live Yield Projections
    telkom_math = engine.project_corridor_yield(discount_rate=0.10, fx_edge_pct=0.05, cycles=5)
    airtel_math = engine.project_corridor_yield(discount_rate=0.06, fx_edge_pct=0.00, cycles=5)
    
    # 2. Build the exact Schema the React UI expects
    opportunities = {
        "telkom_5x": {
            "id": "telkom_5x",
            "title": "Telkom → T-Kash → USDA → ×5 Rollover → Celo",
            "pathDesc": "PATH: N1-N4-N7-N9 \u00A0\u00A0RSK 10% \u00A0\u00A0LIQ 91",
            "profitPct": f"+{telkom_math['projected_profit_pct']}%",
            "discount": "10%",
            "discountNum": 0.10,
            "fxEdge": "5%",
            "pip": "+$0.10",
            "rolloverRate": f"{baseline * 0.95:.2f}",
            "multiplier": f"{telkom_math['single_cycle_multiplier']}×",
            "exitGate": "CYCLE 5",
            "engineTopRight": f"{baseline * 0.95:.2f}",
            "baseline": f"{baseline:.2f}",
            "currency": "USD",
            "nodes": [
                { "id": "N1", "name": "Telkom 10% disc.", "tag": "PROCURE", "color": "blue", "type": "procure" },
                { "id": "N4", "name": "T-Kash Super-Agent", "tag": "LIQUIDATE", "color": "orange", "type": "liquidate" },
                { "id": "N7", "name": "Internal Realization", "tag": "MINT USDA", "color": "emerald", "type": "mint" },
                { "id": "↻", "name": "Cycle 4/5 internal", "tag": "ROLLOVER", "color": "purple", "type": "rollover" },
                { "id": "N9", "name": "Cycle 5 only", "tag": "CELO EXIT", "color": "slate", "type": "exit" }
            ]
        },
        "airtel_5x": {
            "id": "airtel_5x",
            "title": "Airtel → USDA → ×5 Rollover → Celo Exit",
            "pathDesc": "PATH: N2-N7-N9 \u00A0\u00A0RSK 2% \u00A0\u00A0LIQ 98",
            "profitPct": f"+{airtel_math['projected_profit_pct']}%",
            "discount": "6%",
            "discountNum": 0.06,
            "fxEdge": "0%",
            "pip": "+$0.00",
            "rolloverRate": f"{baseline:.2f}",
            "multiplier": f"{airtel_math['single_cycle_multiplier']}×",
            "exitGate": "CYCLE 5",
            "engineTopRight": f"{baseline:.2f}",
            "baseline": f"{baseline:.2f}",
            "currency": "KES",
            "nodes": [
                { "id": "N2", "name": "Airtel 6% disc.", "tag": "PROCURE", "color": "red", "type": "procure" },
                { "id": "N7", "name": "Internal Realization", "tag": "MINT USDA", "color": "blue", "type": "mint" },
                { "id": "↻", "name": "Cycle 4/5 internal", "tag": "ROLLOVER", "color": "purple", "type": "rollover" },
                { "id": "N9", "name": "Cycle 5 only", "tag": "CELO EXIT", "color": "slate", "type": "exit" }
            ]
        }
    }
    
    return {"status": "success", "opportunities": opportunities}