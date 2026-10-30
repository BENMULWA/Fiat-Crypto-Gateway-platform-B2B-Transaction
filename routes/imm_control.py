from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from Brain_Engine.node_registry import (
    CORRIDORS,
    NODES,
    NODE_LEDGER_ASSET,
    corridor_eligible,
    is_corridor_enabled,
    is_node_enabled,
    set_corridor_enabled,
    set_node_enabled,
)
from Brain_Engine.state_engine import ImmutableLedger
from database import get_db, get_imm_switches_col
from routes.auth import get_current_user_with_role, is_admin_role
from admin_audit import log_admin_action

router = APIRouter(prefix="/api/imm", tags=["Internal Market Maker Control"])


def _ensure_admin(current_user: dict):
    if not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Admin role required")


class ToggleRequest(BaseModel):
    enabled: bool


async def _persist_switch(kind: str, entity_id: str, enabled: bool) -> None:
    """Write-through to Mongo so the switch survives a server restart —
    memory_cache (updated by set_node_enabled/set_corridor_enabled) stays
    the hot-path read for the DecisionEngine's tick loop; this collection
    is only consulted once, at startup, to rehydrate that cache."""
    await get_imm_switches_col().update_one(
        {"_id": f"{kind}:{entity_id}"},
        {"$set": {"enabled": enabled}},
        upsert=True,
    )


@router.get("/nodes")
async def list_nodes(current_user=Depends(get_current_user_with_role)):
    """Admin view of every node (N1-N10) and whether it's currently enabled."""
    _ensure_admin(current_user)
    return [
        {
            "id": node.id,
            "label": node.label,
            "assetType": node.asset_type,
            "category": node.category,
            "live": node.live,
            "enabled": is_node_enabled(node.id),
        }
        for node in NODES.values()
    ]


@router.post("/nodes/{node_id}/enabled")
async def toggle_node(
    node_id: str,
    body: ToggleRequest,
    db=Depends(get_db),
    current_user=Depends(get_current_user_with_role),
):
    """Admin pulls a node in/out of service without touching the registry."""
    _ensure_admin(current_user)
    try:
        set_node_enabled(node_id, body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    await _persist_switch("node", node_id, body.enabled)
    await log_admin_action(db, current_user, "imm_node_toggled", nodeId=node_id, enabled=body.enabled)
    return {"status": "success", "nodeId": node_id, "enabled": body.enabled}


@router.get("/corridors")
async def list_corridors(current_user=Depends(get_current_user_with_role)):
    """Admin view of every IMM rollover strategy and whether it's eligible to run."""
    _ensure_admin(current_user)
    return [
        {
            "id": corridor_id,
            **corridor,
            "corridorEnabled": is_corridor_enabled(corridor_id),
            "eligible": corridor_eligible(corridor_id),
        }
        for corridor_id, corridor in CORRIDORS.items()
    ]


@router.post("/corridors/{corridor_id}/enabled")
async def toggle_corridor(
    corridor_id: str,
    body: ToggleRequest,
    db=Depends(get_db),
    current_user=Depends(get_current_user_with_role),
):
    """Admin parks/resumes a whole strategy (e.g. disable TELKOM, keep AIRTEL_LIVE)."""
    _ensure_admin(current_user)
    try:
        set_corridor_enabled(corridor_id, body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    await _persist_switch("corridor", corridor_id, body.enabled)
    await log_admin_action(db, current_user, "imm_corridor_toggled", corridorId=corridor_id, enabled=body.enabled)
    return {"status": "success", "corridorId": corridor_id, "enabled": body.enabled}


@router.get("/health")
async def get_node_health(db=Depends(get_db), current_user=Depends(get_current_user_with_role)):
    """Read-only node health report — same spirit as Comet IMM's
    rebalance/check: it tells you which nodes have breached their floor or
    exposure cap, it never moves anything on its own. Every balance here is
    re-read live from the ImmutableLedger, nothing cached or assumed.

    A node with no entry in NODE_LEDGER_ASSET (N8, N10 today) has never
    been credited/debited by any code path — reported with
    balance: null, not a fabricated 0."""
    _ensure_admin(current_user)
    ledger = ImmutableLedger(db_collection=db["transactions"])

    nodes_report = []
    any_below_floor = False
    any_over_exposure = False

    for node in NODES.values():
        asset = NODE_LEDGER_ASSET.get(node.id)
        balance = await ledger.get_balance(node.id, asset) if asset else None

        below_floor = bool(
            asset is not None and node.min_balance > 0 and balance is not None and balance < node.min_balance
        )
        over_exposure = bool(
            asset is not None and node.exposure_cap_usd > 0 and balance is not None and balance > node.exposure_cap_usd
        )
        any_below_floor = any_below_floor or below_floor
        any_over_exposure = any_over_exposure or over_exposure

        nodes_report.append({
            "id": node.id,
            "label": node.label,
            "asset": asset,
            "balance": balance,
            "minBalance": node.min_balance or None,
            "belowFloor": below_floor,
            "exposureCapUsd": node.exposure_cap_usd or None,
            "overExposureCap": over_exposure,
            "live": node.live,
            "enabled": is_node_enabled(node.id),
        })

    return {
        "status": "success",
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "nodes": nodes_report,
        "anyBelowFloor": any_below_floor,
        "anyOverExposureCap": any_over_exposure,
    }
