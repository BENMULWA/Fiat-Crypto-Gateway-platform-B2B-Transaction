"""Small in-process sliding-window rate limiter, usable as a FastAPI dependency.

    @router.post("/login/request-otp", dependencies=[Depends(rate_limit("login_otp", 10, 300))])

There was no rate limiting anywhere in the API: password guessing, OTP
guessing/flooding (each OTP request sends an email), password-reset spam and
account enumeration through /retail/transfer/lookup were all unbounded.

Scope and limits (be honest about them):
  * State is per process. With N worker processes the effective limit is N times
    higher, and a restart clears it. That still stops scripted bursts, which is
    what matters here; for a multi-instance deployment move the counters to
    Redis (same interface).
  * Keyed by client IP, or by user id for authenticated routes (`by="user"`).
    Behind a proxy the client IP is the RIGHT-most X-Forwarded-For entry (the one
    the platform's own proxy appended); entries to its left are client-supplied
    and spoofable, so they are ignored.

Set RATE_LIMIT_DISABLED=true to switch it off (local load tests).
"""
import os
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_MAX_KEYS = 50_000


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return request.client.host if request.client else "unknown"


def _check(name: str, key: str, limit: int, window_seconds: int) -> None:
    if os.getenv("RATE_LIMIT_DISABLED", "false").strip().lower() == "true":
        return
    now = time.monotonic()
    bucket = _hits[(name, key)]
    while bucket and now - bucket[0] > window_seconds:
        bucket.popleft()
    if len(bucket) >= limit:
        retry_after = max(1, int(window_seconds - (now - bucket[0])))
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait a moment and try again.",
            headers={"Retry-After": str(retry_after)},
        )
    bucket.append(now)

    # Bound memory: drop empty/old buckets if the table grows large.
    if len(_hits) > _MAX_KEYS:
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > window_seconds]:
            _hits.pop(k, None)


def rate_limit(name: str, limit: int, window_seconds: int):
    """Dependency factory: at most `limit` calls per `window_seconds` per client IP."""
    async def _dep(request: Request):
        _check(name, client_ip(request), limit, window_seconds)
    return _dep


def rate_limit_user(name: str, limit: int, window_seconds: int):
    """Same, keyed by the authenticated user id (falls back to IP)."""
    from fastapi import Depends
    from routes.auth import get_current_user

    async def _dep(request: Request, current_user=Depends(get_current_user)):
        _check(name, str((current_user or {}).get("_id") or client_ip(request)), limit, window_seconds)
    return _dep
