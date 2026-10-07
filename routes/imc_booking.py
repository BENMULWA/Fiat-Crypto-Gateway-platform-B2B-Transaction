import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from database import get_db
from routes.auth import get_current_user_with_role, is_admin_role
from services import imc_booking as booking
from services.comet_client import CometClient
from services.imc_receipts import ImcReceipts

router = APIRouter(prefix="/api/imm/mint", tags=["IMC Booking"])
_comet = CometClient()


def _admin(current_user: dict):
    if not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Admin role required")


def _deps(db) -> booking.Deps:
    from routes.valora import get_treasury_address
    return booking.Deps(comet=_comet, db=db, receipts=lambda d: ImcReceipts(d), treasury_address=get_treasury_address())


class BookRequest(BaseModel):
    receipt: str
    cash_kes: Optional[float] = None
    deliver_to: Optional[str] = None
    confirm: bool = False


@router.get("")
async def state(db=Depends(get_db), current_user=Depends(get_current_user_with_role)):
    """Whether booking is configured, the treasury it mints into, and recent bookings. Read-only."""
    _admin(current_user)
    from routes.valora import get_treasury_address
    return {"status": "success", "configured": bool(_comet.api_key and _comet.api_secret), "chain": "celo",
            "treasury": get_treasury_address(), "bookings": await booking.list_bookings(_deps(db))}


@router.get("/job")
async def job(current_user=Depends(get_current_user_with_role)):
    _admin(current_user)
    return {"status": "success", "job": booking.latest_job()}


@router.post("/book", status_code=202)
async def book(req: BookRequest, db=Depends(get_db), current_user=Depends(get_current_user_with_role)):
    """Moves real value: asks Comet to verify the receipt and MINT IMC against it. Requires `confirm: true`.
    Starts in the background and returns a job; follow it with GET /job."""
    _admin(current_user)
    if not req.confirm:
        raise HTTPException(status_code=400, detail="Send confirm: true to book this receipt.")
    if not (_comet.api_key and _comet.api_secret):
        raise HTTPException(status_code=503, detail="COMET_API_KEY and COMET_API_SECRET are not configured on the server.")
    try:
        return {"status": "started", "job": await booking.start(_deps(db), req.receipt, req.cash_kes, req.deliver_to,
                                                                  requested_by=str(current_user.get("_id")))}
    except booking.BookingError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
