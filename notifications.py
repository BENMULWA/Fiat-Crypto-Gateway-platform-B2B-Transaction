"""Shared helper for real, event-driven retail notifications.

Unlike the wallet-threshold alerts in routes/retail.py's
_sync_retail_notifications (upserted/resolved by a fixed code, recomputed on
every /notifications poll), these are one-off events — a deposit landed, a
withdrawal completed or failed, a swap finished, an airtime redemption
failed, KYC was reviewed. Each gets its own document so it isn't clobbered
by the threshold-alert resync, and is pushed live over the existing
/ws/dashboard websocket (see broadcast.py) so the bell updates without
waiting for its next 30s poll.
"""
import uuid
from datetime import datetime

from broadcast import broadcast_manager

try:
    from bson import ObjectId
except ImportError:
    ObjectId = None


def _normalize_user_id(val):
    """Match the ObjectId type _sync_retail_notifications/get_retail_notifications
    already store/query with, regardless of what type the caller has on hand —
    this codebase has a recurring bug class of str/ObjectId userId mismatches."""
    if ObjectId and isinstance(val, str) and len(val) == 24:
        try:
            return ObjectId(val)
        except Exception:
            pass
    return val


async def notify_user(
    db,
    user_id,
    category: str,
    severity: str,
    title: str,
    message: str,
    extra: dict | None = None,
) -> None:
    """Persist a notification for the bell and push it live over the websocket.

    Best-effort on both halves: a notification failure must never break the
    deposit/withdrawal/swap/KYC flow that triggered it.
    """
    normalized_id = _normalize_user_id(user_id)
    now = datetime.utcnow()
    doc = {
        "_id": f"EVT_{uuid.uuid4().hex[:12].upper()}",
        "userId": normalized_id,
        "category": category,
        "severity": severity,
        "title": title,
        "message": message,
        "isRead": False,
        "resolved": False,
        "createdAt": now,
        "updatedAt": now,
    }
    if extra:
        doc.update(extra)

    try:
        await db["retail_notifications"].insert_one(doc)
    except Exception:
        pass

    try:
        await broadcast_manager.send_user(str(user_id), {
            "type": "notification",
            "category": category,
            "severity": severity,
            "title": title,
            "message": message,
        })
    except Exception:
        pass
