import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from database import get_db
from routes.auth import get_current_user_with_role, is_admin_role
from services.imc_receipts import AttachError, HeldError, ImcReceipts

router = APIRouter(prefix="/api/imm/imc", tags=["IMC Receipts"])


def _admin(current_user: dict):
    if not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Admin role required")


def _public(r: dict) -> dict:
    return {
        "id": r["_id"], "txHash": r["txHash"], "logIndex": r["logIndex"], "blockNumber": r["blockNumber"],
        "from": r["from"], "to": r["to"], "amountImc": r["amountImc"],
        "blockTime": r["blockTime"].isoformat() + "Z" if r.get("blockTime") else None,
        "claimedByRun": r.get("claimedByRun"), "consumed": bool(r.get("consumed")), "source": r.get("source"),
    }


@router.get("/receipts")
async def list_receipts(limit: int = 20, db=Depends(get_db), current_user=Depends(get_current_user_with_role)):
    """IMC received at the treasury wallet, newest first. The chain is scanned first, so a transfer
    that just landed shows up without waiting for the background watcher."""
    _admin(current_user)
    svc = ImcReceipts(db)
    try:
        await asyncio.wait_for(svc.scan(), timeout=20)
    except Exception:
        pass  # show what is already recorded rather than failing the list
    return {"treasury": svc.treasury, "receipts": [_public(r) for r in await svc.list_receipts(min(max(limit, 1), 100))]}


class HeldRequest(BaseModel):
    run_id: str


@router.post("/allocate-held")
async def allocate_held(req: HeldRequest, db=Depends(get_db), current_user=Depends(get_current_user_with_role)):
    """Let a run that is waiting for its manual mint use IMC the treasury already holds instead. Books
    exactly the run's expected amount, and only from IMC no other run has used. For testing the flow end
    to end without a new mint."""
    _admin(current_user)
    run = await db["corridor_runs"].find_one({"_id": req.run_id})
    if not run:
        raise HTTPException(status_code=404, detail="Unknown corridor run ID")
    if run.get("status") != "AWAITING_MANUAL_MINT":
        raise HTTPException(status_code=409, detail=f"Run {req.run_id} is {run.get('status')}, not waiting for a manual mint.")
    try:
        receipt = await ImcReceipts(db).allocate_held(req.run_id, float(run.get("pendingMintExpectedImc") or 0))
    except HeldError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "allocated", "receipt": _public(receipt),
            "message": f"{receipt['amountImc']:,.6f} IMC allocated from holdings. The run continues within seconds."}


class AttachRequest(BaseModel):
    run_id: str
    tx_hash: str


@router.post("/attach")
async def attach_mint(req: AttachRequest, db=Depends(get_db), current_user=Depends(get_current_user_with_role)):
    """Reserve an on-chain IMC transfer for a run that is waiting for its manual mint. The transaction is
    verified on Celo first; its amount must match what the run expects (within 1%)."""
    _admin(current_user)
    run = await db["corridor_runs"].find_one({"_id": req.run_id})
    if not run:
        raise HTTPException(status_code=404, detail="Unknown corridor run ID")
    if run.get("status") != "AWAITING_MANUAL_MINT":
        raise HTTPException(status_code=409, detail=f"Run {req.run_id} is {run.get('status')}, not waiting for a manual mint.")
    expected: Optional[float] = run.get("pendingMintExpectedImc")
    if not expected:
        raise HTTPException(status_code=409, detail="This run has no expected mint amount recorded.")
    try:
        receipt = await ImcReceipts(db).attach(req.run_id, req.tx_hash, float(expected))
    except AttachError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "attached", "receipt": _public(receipt), "message": "The run will book this mint within seconds."}
