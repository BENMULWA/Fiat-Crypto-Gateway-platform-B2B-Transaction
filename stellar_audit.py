"""Append-only audit log for every real Stellar signing/broadcast event —
withdrawals, provisioning, sweeps. Mirrors celo_audit.py's shape/purpose,
kept as a separate collection per chain so an incident investigation never
depends on another chain's bookkeeping staying consistent.
"""
import uuid
from datetime import datetime


async def log_stellar_audit_event(db, event: str, **fields) -> dict:
    doc = {
        "_id": f"AUDIT_{uuid.uuid4().hex[:12].upper()}",
        "event": event,
        "chain": "stellar",
        "createdAt": datetime.utcnow(),
        **fields,
    }
    try:
        await db["stellar_audit_log"].insert_one(doc)
    except Exception:
        # Audit logging must never block or fail a money-moving operation — but a
        # failure here is itself worth alerting on separately (it's a visibility
        # gap, not a fund-safety one).
        pass
    return doc
