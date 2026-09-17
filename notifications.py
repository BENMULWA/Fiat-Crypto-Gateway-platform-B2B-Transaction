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
import asyncio
import uuid
from datetime import datetime

from broadcast import broadcast_manager
from transaction_email import send_transaction_email

# Categories that also get an email — the same "Binance/Coinstore" pattern
# of a push/in-app alert plus an email receipt, starting with the two event
# types users actually asked to be emailed about. Swap/KYC/etc. stay
# in-app-only for now; add a category here to extend email coverage.
EMAIL_NOTIFIED_CATEGORIES = {"deposit", "withdrawal"}

# asyncio only holds a *weak* reference to a task created via create_task —
# without keeping a real reference somewhere, it can be garbage-collected
# mid-flight ("Task was destroyed but it is pending"). This set is that
# reference; each task removes itself once done.
_pending_email_tasks: set = set()

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

    if category in EMAIL_NOTIFIED_CATEGORIES:
        try:
            user_doc = await db["users"].find_one(
                {"_id": normalized_id}, {"email": 1, "antiPhishingCode": 1}
            )
            recipient = (user_doc or {}).get("email")
            if recipient:
                # SMTP is blocking; run it off the event loop so a slow/stuck
                # mail server can't stall the deposit/withdrawal request path
                # that's awaiting this notify_user() call.
                task = asyncio.create_task(
                    asyncio.to_thread(
                        send_transaction_email,
                        recipient,
                        category,
                        severity,
                        title,
                        message,
                        (user_doc or {}).get("antiPhishingCode", ""),
                        extra,
                    )
                )
                _pending_email_tasks.add(task)
                task.add_done_callback(_pending_email_tasks.discard)
        except Exception:
            pass
