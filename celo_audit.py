"""Append-only audit log for every real Celo signing/broadcast event —
withdrawals, gas top-ups, sweeps. Kept separate from `ramp_entries` (business
records meant for user-facing history) so a security review or incident
investigation doesn't depend on business-logic bookkeeping staying consistent;
this log's only job is "what did we sign and broadcast, and why."
"""
import uuid
from datetime import datetime


async def log_celo_audit_event(db, event: str, **fields) -> dict:
    doc = {
        "_id": f"AUDIT_{uuid.uuid4().hex[:12].upper()}",
        "event": event,
        "chain": "celo",
        "createdAt": datetime.utcnow(),
        **fields,
    }
    try:
        await db["celo_audit_log"].insert_one(doc)
    except Exception:
        # Audit logging must never block or fail a money-moving operation — but a
        # failure here is itself worth alerting on separately (it's a visibility
        # gap, not a fund-safety one).
        pass
    return doc
