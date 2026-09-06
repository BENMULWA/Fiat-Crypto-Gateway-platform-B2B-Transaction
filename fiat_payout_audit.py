"""Append-only audit log for real fiat bank/card payout events (Flutterwave
transfers). Mirrors celo_audit.py/stellar_audit.py's shape — a separate
collection per payout rail so an incident investigation never depends on
another rail's bookkeeping staying consistent.
"""
import uuid
from datetime import datetime


async def log_fiat_payout_audit_event(db, event: str, **fields) -> dict:
    doc = {
        "_id": f"AUDIT_{uuid.uuid4().hex[:12].upper()}",
        "event": event,
        "rail": "flutterwave_bank",
        "createdAt": datetime.utcnow(),
        **fields,
    }
    try:
        await db["fiat_payout_audit_log"].insert_one(doc)
    except Exception:
        # Audit logging must never block or fail a money-moving operation — but a
        # failure here is itself worth alerting on separately (it's a visibility
        # gap, not a fund-safety one).
        pass
    return doc
