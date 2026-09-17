import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from Brain_Engine.cache import memory_cache

from Brain_Engine.Discovery_Engine import IMMDiscoveryEngine
from Brain_Engine.node_registry import corridor_eligible
from Brain_Engine.risk_engine import is_rate_stale, rate_age_seconds, RATE_STALE_AFTER_SECONDS
from services.comet_client import CometClient
from routes.auth import get_current_user_with_role, is_admin_role
from routes.treasury import _read_celo_usdc_balance_sync


router = APIRouter(prefix="/api/market-maker", tags=["Market Maker API"])
comet = CometClient()


def _ensure_admin(current_user: dict):
    if not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Admin role required")

class SpreadUpdate(BaseModel):
    active: bool
    autoPeg: bool
    bid: float
    ask: float

@router.get("/spread")
async def get_spread_config():
    """Fetches the live pricing configuration from the fast-memory cache.

    Includes staleness: this rate is exactly what an admin last set, full
    stop — no oracle, no market feed behind it. updatedAt is None (and
    stale is True) until an admin has explicitly called POST /spread at
    least once; an unset rate is exactly as untrustworthy as a stale one."""
    updated_at = memory_cache.get("spread:usda_kes:updated_at")
    return {
        "active": memory_cache.get("spread:usda_kes:active") if memory_cache.get("spread:usda_kes:active") is not None else True,
        "autoPeg": memory_cache.get("spread:usda_kes:auto_peg") if memory_cache.get("spread:usda_kes:auto_peg") is not None else True,
        "bid": memory_cache.get("spread:usda_kes:bid") or 128.00,
        "ask": memory_cache.get("spread:usda_kes:ask") or 132.00,
        "reference": memory_cache.get("rates:binance_usdt_kes") or 130.50,
        "updatedAt": updated_at,
        "ageSeconds": rate_age_seconds(updated_at),
        "stale": is_rate_stale(updated_at),
        "staleAfterSeconds": RATE_STALE_AFTER_SECONDS,
    }

@router.post("/spread")
async def update_spread_config(config: SpreadUpdate):
    """Admin updates the spread. Instantly applied to all retail quotes."""
    memory_cache.set("spread:usda_kes:active", config.active)
    memory_cache.set("spread:usda_kes:auto_peg", config.autoPeg)
    memory_cache.set("spread:usda_kes:bid", config.bid)
    memory_cache.set("spread:usda_kes:ask", config.ask)
    memory_cache.set("spread:usda_kes:updated_at", datetime.now(timezone.utc).isoformat())

    return {"status": "success", "message": "Spread parameters updated globally!"}


@router.get("/spread/comet")
async def get_comet_spread(base: str = "KES", quote: str = "IMC", amount_in: float = 1.0):
    """Live KES/IMC pricing sourced from Comet Engine's IMM (docs.mamlakapsp.com)
    instead of an admin-typed number — this is real-time market making, not a
    static spread. Comet enforces its own 60-minute rate-staleness rule and
    refuses the quote itself if nobody has called POST /admin/imm/rates
    recently; we surface that error as-is rather than silently falling back,
    since a fallback here would hide exactly the staleness Comet is designed
    to catch."""
    result = comet.get_imm_quote(base, quote, amount_in)
    if result["status"] == "error":
        raise HTTPException(status_code=502, detail=f"Comet IMM quote failed: {result['message']}")
    return {"status": "success", "provider": "comet", **result["data"]}


@router.get("/exposure/comet")
async def get_comet_exposure(chain: str = "celo", asset: str = "IMC"):
    """Live vault-backed vs. synthetic exposure for one Comet-tracked asset —
    the risk picture your own node_registry.py exposure_cap_usd fields don't
    have today, since they only bound your local ledger, not real reserves."""
    result = comet.get_exposure(chain, asset)
    if result["status"] == "error":
        raise HTTPException(status_code=502, detail=f"Comet exposure check failed: {result['message']}")
    return {"status": "success", "provider": "comet", **result["data"]}


@router.get("/n9-reference/comet")
async def get_n9_comet_reference(to_symbol: str = "USDT"):
    """Read-only reference price for the corridor's real Celo exit leg
    (node_registry.py's N9 — 'Celo Exit', live=True — same real balance as
    treasury.py's CELO_USDC, NOT the frontend's separate 'N9' Stellar
    router card). Prices your actual current on-chain USDC balance through
    Comet's live Celo AMM pools — no execution, no vault, nothing moved.
    This is the join point we scoped: N9 already produces a real
    Comet-registered asset (USDC), so it's the one place Comet's pricing
    is meaningful today without re-platforming anything else."""
    try:
        usdc_balance = await asyncio.wait_for(asyncio.to_thread(_read_celo_usdc_balance_sync), timeout=20)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not read real Celo USDC balance: {exc}")

    amount_base = str(int(round(usdc_balance * 10**6)))  # USDC = 6 decimals on Celo
    result = comet.get_amm_quote("USDC", to_symbol, amount_base)
    if result["status"] == "error":
        raise HTTPException(status_code=502, detail=f"Comet AMM quote failed: {result['message']}")

    data = result["data"]
    amount_out_base = float(data.get("amountOut", 0) or 0)
    return {
        "status": "success",
        "provider": "comet",
        "node": "N9",
        "realUsdcBalance": usdc_balance,
        "quoteTo": to_symbol,
        "quotedOut": amount_out_base / 10**6,
        "raw": data,
    }


class ExecuteCometSwapRequest(BaseModel):
    external_user_id: str
    base: str
    quote: str
    amount_in: float
    chain: str = "celo"


@router.post("/execute-swap/comet")
async def execute_comet_swap(
    req: ExecuteCometSwapRequest,
    current_user=Depends(get_current_user_with_role),
):
    """Executes a real swap through Comet's IMM vault — same gating as
    treasury.py's admin-only money-movement routes. `externalId` is
    generated server-side (never caller-supplied) so a retried/duplicated
    HTTP request can't be replayed into a second real swap; Comet's own
    idempotency keys off this value.

    Response passes through Comet's `vaultBacked` flag untouched — that's
    the honest signal for whether this payout came from real reserves or
    a synthetic mint, and the caller/UI must not paper over it."""
    _ensure_admin(current_user)

    if req.amount_in <= 0:
        raise HTTPException(status_code=400, detail="amount_in must be greater than zero.")

    external_id = f"IMM_SWAP_{uuid.uuid4().hex[:12].upper()}"
    result = comet.execute_imm_swap(
        external_user_id=req.external_user_id,
        chain=req.chain,
        base=req.base,
        quote=req.quote,
        amount_in=req.amount_in,
        external_id=external_id,
    )
    if result["status"] == "error":
        raise HTTPException(status_code=502, detail=f"Comet IMM swap failed: {result['message']}")

    return {
        "status": "success",
        "provider": "comet",
        "externalId": external_id,
        "requestedBy": str(current_user.get("_id")),
        **result["data"],
    }

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
    
    # Hide corridors the admin has switched off (per-corridor or via one of
    # its nodes) so the dealer's "Deploy" dropdown can't offer them at all —
    # the hard enforcement lives in treasury.py's /corridor/execute-hft.
    opportunities = {k: v for k, v in opportunities.items() if corridor_eligible(k)}

    return {"status": "success", "opportunities": opportunities}