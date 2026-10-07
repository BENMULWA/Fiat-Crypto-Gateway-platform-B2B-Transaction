from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from Brain_Engine.node_registry import CORRIDORS, PROCUREMENT_WALLETS
from database import get_db
from routes.auth import get_current_user_with_role, is_admin_role
from services import impala_float as float_svc

router = APIRouter(prefix="/api/imm/float", tags=["Impala Float"])


def _admin(current_user: dict):
    if not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Admin role required")


def _commission() -> float:
    # The reseller commission is one number for the account; the live Airtel corridor carries it.
    return float(CORRIDORS["airtel_5x"]["discount"])


class TopupRequest(BaseModel):
    amount: int
    paying_phone: Optional[str] = None  # defaults to the configured N2 procurement phone


def _public(doc: dict) -> dict:
    """The stored record, shaped for the console. Dates are returned as ISO strings."""
    d = dict(doc)
    d["id"] = d.pop("_id")
    for k in ("createdAt", "updatedAt"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat() + "Z"
    return d


@router.get("")
async def float_state(current_user=Depends(get_current_user_with_role)):
    """Current float balance, the paybill details for a manual top-up, and the limits."""
    _admin(current_user)
    try:
        return {"status": "success", **await float_svc.get_state(
            float_svc.impala_airtime, commission=_commission(), default_phone=PROCUREMENT_WALLETS.get("N2") or None)}
    except float_svc.ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"The configured procurement phone is invalid: {exc}")


@router.post("/topup", status_code=201)
async def start_topup(req: TopupRequest, db=Depends(get_db), current_user=Depends(get_current_user_with_role)):
    """Sends a real M-Pesa STK push. The float is credited only after the owner of the paying
    phone approves it, so the response is a PENDING record to follow with GET /topups/{id}."""
    _admin(current_user)
    try:
        doc = await float_svc.start_topup(
            db,
            amount_kes=req.amount,
            paying_phone=req.paying_phone,
            default_phone=PROCUREMENT_WALLETS.get("N2") or None,
            commission=_commission(),
            requested_by=str(current_user.get("_id")),
        )
    except float_svc.TopupConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except float_svc.ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "pending", "topup": _public(doc)}


@router.get("/topups")
async def list_topups(limit: int = 20, db=Depends(get_db), current_user=Depends(get_current_user_with_role)):
    _admin(current_user)
    docs = await float_svc.list_topups(db, limit=min(max(limit, 1), 100))
    return {"topups": [_public(d) for d in docs]}


@router.post("/topups/{topup_id}/cancel")
async def cancel_topup(topup_id: str, db=Depends(get_db), current_user=Depends(get_current_user_with_role)):
    """Stops waiting for a pending top-up (for example when no STK prompt arrived). A credit that has
    already landed settles the top-up instead of cancelling it."""
    _admin(current_user)
    try:
        doc = await float_svc.cancel_topup(db, topup_id, cancelled_by=str(current_user.get("_id")))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"status": "cancelled", "topup": _public(doc)}


@router.get("/topups/{topup_id}")
async def get_topup(topup_id: str, db=Depends(get_db), current_user=Depends(get_current_user_with_role)):
    """Re-reads the real balance and settles the record if the credit has landed."""
    _admin(current_user)
    doc = await float_svc.get_topup(db, topup_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Unknown top-up id")
    return _public(doc)
