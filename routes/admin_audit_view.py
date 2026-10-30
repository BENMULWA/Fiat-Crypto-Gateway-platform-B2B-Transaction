"""Admin-only read access to the platform's audit trails and "stuck money" queue.

Before this existed the audit collections (celo_audit_log, stellar_audit_log,
fiat_payout_audit_log, compliance_checks, airtel_callback_events,
internal_transfers) could only be read by querying MongoDB directly, internal
transfers did not appear in the admin dashboard at all, and nothing surfaced a
request that died halfway (debited but never credited/refunded).

  GET /api/admin/audit/events   one feed per source, newest first, with search + paging
  GET /api/admin/audit/stuck    everything that needs a human: stuck intents,
                                unconfirmed/ambiguous payouts, transfers needing
                                reconciliation, compliance holds, failed swaps
  GET /api/admin/audit/summary  counts for the dashboard badge
"""
import re
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from database import get_db
from routes.auth import get_current_user_with_role, is_admin_role

router = APIRouter(prefix="/api/admin/audit", tags=["Admin Audit"])

# source key -> (collection, field used for ordering, fields never sent to the browser)
SOURCES: dict[str, tuple[str, str, set[str]]] = {
    "admin_actions": ("admin_audit_log", "createdAt", set()),
    "ramp_intents": ("ramp_intents", "createdAt", set()),
    "transfers": ("internal_transfers", "createdAt", set()),
    "celo": ("celo_audit_log", "createdAt", set()),
    "stellar": ("stellar_audit_log", "createdAt", set()),
    "payouts": ("fiat_payout_audit_log", "createdAt", set()),
    "compliance": ("compliance_checks", "created_at", {"raw_response"}),
    # Raw bodies and request headers can contain provider secrets/PII; the
    # parsed payload + auth verdict is what an investigation needs.
    "callbacks": ("airtel_callback_events", "receivedAt", {"rawBody", "headers"}),
}

STUCK_INTENT_MINUTES = 5


def _ensure_admin(current_user: dict) -> None:
    if not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Admin role required")


def _jsonable(value: Any) -> Any:
    """Make a Mongo document safe for JSON (ObjectId, datetime, nested)."""
    if isinstance(value, datetime):
        return value.isoformat() + "Z"
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _clean(doc: dict, hidden: set[str]) -> dict:
    return _jsonable({k: v for k, v in doc.items() if k not in hidden})


@router.get("/events")
async def audit_events(
    source: str = "admin_actions",
    q: str | None = None,
    limit: int = 50,
    skip: int = 0,
    db=Depends(get_db),
    current_user=Depends(get_current_user_with_role),
):
    _ensure_admin(current_user)
    if source not in SOURCES:
        raise HTTPException(status_code=400, detail=f"Unknown source. Choose one of: {', '.join(SOURCES)}")
    collection, order_field, hidden = SOURCES[source]
    limit = max(1, min(int(limit or 50), 200))
    skip = max(0, int(skip or 0))

    query: dict = {}
    if q:
        # Escaped: this value is user-typed and goes into a $regex.
        pattern = re.escape(q.strip()[:100])
        text_fields = ["_id", "action", "event", "status", "userId", "actorEmail", "asset", "txId",
                       "senderEmail", "recipientEmail", "trade_id", "user_id", "outcome", "path"]
        query = {"$or": [{f: {"$regex": pattern, "$options": "i"}} for f in text_fields]}

    cursor = db[collection].find(query).sort(order_field, -1).skip(skip).limit(limit)
    docs = await cursor.to_list(length=limit)
    total = await db[collection].count_documents(query)
    return {
        "status": "success", "source": source, "total": total, "skip": skip, "limit": limit,
        "events": [_clean(d, hidden) for d in docs],
    }


async def _collect_stuck(db) -> dict:
    now = datetime.utcnow()
    stale = now - timedelta(minutes=STUCK_INTENT_MINUTES)
    hour_ago = now - timedelta(hours=1)

    async def fetch(collection: str, query: dict, order: str = "createdAt", hidden: set[str] | None = None, n: int = 100):
        docs = await db[collection].find(query).sort(order, -1).limit(n).to_list(length=n)
        return [_clean(d, hidden or set()) for d in docs]

    return {
        # Request began, never finished: the process died or hung after a debit.
        "stuckIntents": await fetch("ramp_intents", {"status": "started", "createdAt": {"$lte": stale}}),
        # Withdrawal sent to the provider with no confirmation either way.
        "unconfirmedPayouts": await fetch(
            "ramp_entries",
            {"direction": "off", "status": {"$in": ["pending", "processing"]},
             "$or": [{"needsReview": True}, {"ambiguousProvider": True}]},
            hidden={"providerReport", "gatewayResponse", "gatewayResponseText"},
        ),
        # Internal transfer where the debit happened but credit/reversal didn't complete.
        "transfersNeedingReconciliation": await fetch(
            "internal_transfers",
            {"$or": [{"status": "needs_reconciliation"}, {"status": "started", "createdAt": {"$lte": stale}}]},
        ),
        # Held for compliance and not yet released.
        "complianceHolds": await fetch("ramp_entries", {"status": "pending_compliance_review"}),
        # Money-moving requests that failed in the last hour (5xx = failed mid-flight).
        "recentFailures": await fetch("ramp_intents", {"status": {"$in": ["failed", "error"]}, "createdAt": {"$gte": hour_ago}}),
        # Callbacks that were rejected or that blew up while processing.
        "badCallbacks": await fetch(
            "airtel_callback_events",
            {"receivedAt": {"$gte": hour_ago}, "$or": [{"error": {"$exists": True}}, {"processed": False}]},
            order="receivedAt", hidden={"rawBody", "headers"}, n=50,
        ),
    }


@router.get("/stuck")
async def stuck_items(db=Depends(get_db), current_user=Depends(get_current_user_with_role)):
    _ensure_admin(current_user)
    data = await _collect_stuck(db)
    return {"status": "success", "counts": {k: len(v) for k, v in data.items()}, **data}


@router.get("/summary")
async def audit_summary(db=Depends(get_db), current_user=Depends(get_current_user_with_role)):
    _ensure_admin(current_user)
    data = await _collect_stuck(db)
    counts = {k: len(v) for k, v in data.items()}
    return {"status": "success", "counts": counts, "needsAttention": sum(counts.values())}
