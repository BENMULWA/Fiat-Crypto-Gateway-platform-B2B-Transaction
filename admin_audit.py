"""Append-only audit log for privileged admin/operator actions — who did what,
to what, and when. Sibling of celo_audit.py / stellar_audit.py /
fiat_payout_audit.py, which record what was *signed on a chain or sent to a
payout rail*; this one records the *human decision* behind it (kill switch
flipped, spread changed, revenue cashed out, sandbox reset, ...), so an
incident review can answer "who changed that" and not just "what moved".

Never raises: an audit-write failure must not block the admin action itself,
but it is a visibility gap worth alerting on separately.
"""
import uuid
from datetime import datetime


async def log_admin_action(db, current_user: dict | None, action: str, **fields) -> dict:
    doc = {
        "_id": f"ADMIN_{uuid.uuid4().hex[:12].upper()}",
        "action": action,
        "actorId": str((current_user or {}).get("_id") or ""),
        "actorEmail": (current_user or {}).get("email"),
        "actorRole": (current_user or {}).get("role"),
        "createdAt": datetime.utcnow(),
        **fields,
    }
    try:
        await db["admin_audit_log"].insert_one(doc)
    except Exception:
        pass
    return doc
