from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from database import get_db
from routes.auth import get_current_user_with_role, is_admin_role
from services import rate_feed

router = APIRouter(prefix="/api/base-rate", tags=["Base Rate"])


def _ensure_admin(current_user: dict):
    if not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Admin role required")


class FixRateRequest(BaseModel):
    rate: float
    source: str = "manual"


@router.get("")
async def get_base_rate_status():
    """Read-only: current KES/USD rate, who fixed it, when, and whether
    it's stale. No auth required — this mirrors /api/market-maker/spread's
    own openness, since it's informational, not money-moving."""
    return {"status": "success", **rate_feed.get_rate_status()}


@router.post("/fix")
async def fix_base_rate(
    body: FixRateRequest,
    db=Depends(get_db),
    current_user=Depends(get_current_user_with_role),
):
    """Admin fixes today's KES/USD base rate. This is the one dial that
    feeds: (a) main.py's Comet KES/IMC peg refresh, (b) the live-discount
    floor math in Brain_Engine/state_engine.py, and (c) the KES-exit path's
    valuation — fixing it here is what those all read from instead of each
    hardcoding their own copy of the number."""
    _ensure_admin(current_user)
    if body.rate <= 0:
        raise HTTPException(status_code=400, detail="Rate must be greater than zero.")

    doc = await rate_feed.fix_base_rate(
        db, rate=body.rate, fixed_by=str(current_user.get("_id")), source=body.source
    )
    return {"status": "success", "data": doc}
