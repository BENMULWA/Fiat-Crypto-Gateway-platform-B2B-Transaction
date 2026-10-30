"""Per-user, per-operation mutual exclusion backed by MongoDB.

Why: /ramp/execute, withdrawals and transfers read a balance, call a provider
or a chain, then write results -- none of it inside a database transaction. Two
overlapping requests from one user (a double-click, a retry on a slow
connection, a scripted burst) both pass the balance check and both execute.
Holding this lock for the duration of the request makes the second one fail
fast with 429 instead of racing the first.

A MongoDB document (not an in-process asyncio.Lock) so it also holds across
multiple workers/instances. A lock that is never released (process killed
mid-request) expires after `ttl_seconds` and can then be taken over.
"""
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

_COLLECTION = "request_locks"


async def acquire_user_lock(db, user_id, name: str, ttl_seconds: int = 300) -> str:
    """Take the lock or raise HTTP 429. Returns a token to pass to release."""
    lock_id = f"{name}:{user_id}"
    token = uuid.uuid4().hex
    now = datetime.utcnow()
    doc = {"_id": lock_id, "token": token, "acquiredAt": now, "expiresAt": now + timedelta(seconds=ttl_seconds)}
    try:
        await db[_COLLECTION].insert_one(doc)
        return token
    except DuplicateKeyError:
        pass

    # Someone holds it. Take over only if their lock has expired (crashed holder).
    taken = await db[_COLLECTION].find_one_and_update(
        {"_id": lock_id, "expiresAt": {"$lt": now}},
        {"$set": {k: v for k, v in doc.items() if k != "_id"}},
    )
    if taken:
        return token
    raise HTTPException(
        status_code=429,
        detail="Another transaction on your account is still in progress. Please wait a moment and try again.",
    )


async def release_user_lock(db, user_id, name: str, token: str) -> None:
    """Release only if we still own it (token match). Never raises."""
    try:
        await db[_COLLECTION].delete_one({"_id": f"{name}:{user_id}", "token": token})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# FastAPI dependency: one money-moving request per user at a time
# ---------------------------------------------------------------------------
# Imported lazily inside the function so this module stays importable without
# routes.auth (and so there can never be an import cycle with it).
MONEY_OUT = "money_out"


def single_flight(name: str = MONEY_OUT):
    """Use as `@router.post(..., dependencies=[Depends(single_flight())])`.

    Every route that moves value out of a user's balance shares the same lock
    name by default, so a user also can't run a withdrawal, a transfer and a swap
    in parallel to get several requests past the same balance check."""
    from fastapi import Depends
    from database import get_db
    from routes.auth import get_current_user

    async def _dep(db=Depends(get_db), current_user=Depends(get_current_user)):
        user_key = str(current_user["_id"])
        token = await acquire_user_lock(db, user_key, name)
        try:
            yield
        finally:
            await release_user_lock(db, user_key, name, token)

    return _dep
