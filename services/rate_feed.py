"""Live KES/USD base-rate feed — the single source of truth that
`main.py`'s Comet KES/IMC peg refresh and the Comet-backed corridor paths
(Brain_Engine/state_engine.py) read from, instead of each hardcoding its
own copy of a number like 129.50.

There is no automated external market-data source wired in here yet — same
as Comet's own IMM rate (POST /api/v1/admin/imm/rates), the base rate is
fixed by an admin action (fix_base_rate) rather than auto-fetched. Building
a real scraper/subscription against a CBK feed is a separate, later piece;
faking one here would be worse than being explicit that this is manual for
now. A caller that wires in a real feed later should still write through
fix_base_rate() (source="cbk_feed" or similar) so staleness tracking and
the memory_cache hot path keep working unchanged.

Same durability pattern as node_registry's admin switches: memory_cache is
the hot-path read (every corridor tick), the `base_rates` Mongo collection
is the durable copy rehydrated into memory_cache once at server startup
(see main.py) so a restart doesn't silently forget today's fixed rate.
"""

from datetime import datetime, timezone
from typing import Optional

from Brain_Engine.cache import memory_cache
from Brain_Engine.risk_engine import is_rate_stale, rate_age_seconds, RATE_STALE_AFTER_SECONDS

# Reuses the "rates:cbk_kes_usd" key Brain_Engine/cache.py already seeds
# with a placeholder (129.80) — that slot was reserved for exactly this and
# never actually wired to anything before now (confirmed: nothing besides
# cache.py's own initializer wrote or read it). This module is what makes
# it real instead of a permanent placeholder.
_CACHE_KEY_RATE = "rates:cbk_kes_usd"
_CACHE_KEY_UPDATED_AT = "rates:cbk_kes_usd:updated_at"
_CACHE_KEY_SOURCE = "rates:cbk_kes_usd:source"
_CACHE_KEY_FIXED_BY = "rates:cbk_kes_usd:fixed_by"

_DOC_ID = "KES_USD"


class BaseRateUnavailable(Exception):
    """Raised by get_base_rate() when no admin has ever fixed a rate and
    the caller passed no default — deliberately not a silent fallback to a
    hardcoded literal, since that's exactly the bug this module exists to
    remove."""


def get_base_rate(default: Optional[float] = None, allow_stale: bool = False) -> float:
    """Returns the currently fixed KES/USD rate. Raises BaseRateUnavailable
    if nobody has ever called fix_base_rate() (and no `default` was given),
    or if the fix is older than RATE_STALE_AFTER_SECONDS and `allow_stale`
    is False — deliberately NOT a presence check alone: Brain_Engine/cache.py
    pre-seeds this same cache key with a 129.80 placeholder at process
    start, so a raw "is it set" check would always pass even though nobody
    ever actually fixed it. Staleness (is_rate_stale treats an unset
    updated_at exactly like an old one) is the real gate here.

    Callers migrating off a hardcoded constant should pass that constant as
    `default` during the transition, then drop it once an admin has fixed a
    real rate at least once in every environment that needs it."""
    updated_at = memory_cache.get(_CACHE_KEY_UPDATED_AT)
    if is_rate_stale(updated_at):
        if default is not None and not allow_stale:
            return default
        if not allow_stale:
            raise BaseRateUnavailable(
                "No fresh KES/USD base rate on record (never fixed, or older than "
                f"{RATE_STALE_AFTER_SECONDS:.0f}s) — call POST /api/base-rate/fix "
                "(or rate_feed.fix_base_rate) before running anything that prices off it."
            )

    value = memory_cache.get(_CACHE_KEY_RATE)
    if value is None:
        if default is not None:
            return default
        raise BaseRateUnavailable("No KES/USD base rate is set at all, not even the cache placeholder.")
    return float(value)


def get_rate_status() -> dict:
    """Full status for admin UI / pre-flight checks: value, who set it,
    when, and whether it's past RATE_STALE_AFTER_SECONDS."""
    updated_at = memory_cache.get(_CACHE_KEY_UPDATED_AT)
    return {
        "rate": memory_cache.get(_CACHE_KEY_RATE),
        "source": memory_cache.get(_CACHE_KEY_SOURCE),
        "fixedBy": memory_cache.get(_CACHE_KEY_FIXED_BY),
        "updatedAt": updated_at,
        "ageSeconds": rate_age_seconds(updated_at),
        "stale": is_rate_stale(updated_at),
        "staleAfterSeconds": RATE_STALE_AFTER_SECONDS,
    }


async def fix_base_rate(db, rate: float, fixed_by: str, source: str = "manual") -> dict:
    """Admin action: fixes today's KES/USD rate. Writes memory_cache first
    (so the very next corridor tick sees it) then persists to Mongo (so a
    restart doesn't lose it) — same order as node_registry's
    set_node_enabled/_persist_switch split in routes/imm_control.py."""
    if rate <= 0:
        raise ValueError("Base rate must be greater than zero.")

    now_iso = datetime.now(timezone.utc).isoformat()

    memory_cache.set(_CACHE_KEY_RATE, float(rate))
    memory_cache.set(_CACHE_KEY_UPDATED_AT, now_iso)
    memory_cache.set(_CACHE_KEY_SOURCE, source)
    memory_cache.set(_CACHE_KEY_FIXED_BY, fixed_by)

    doc = {
        "_id": _DOC_ID,
        "rate": float(rate),
        "source": source,
        "fixedBy": fixed_by,
        "updatedAt": now_iso,
    }
    await db["base_rates"].update_one({"_id": _DOC_ID}, {"$set": doc}, upsert=True)
    return doc


async def rehydrate_from_db(db) -> None:
    """Called once at server startup (main.py, same spot the imm_switches
    rehydration already runs) so a restart doesn't silently forget the last
    fixed rate and fall back to BaseRateUnavailable until an admin re-fixes
    it by hand."""
    doc = await db["base_rates"].find_one({"_id": _DOC_ID})
    if not doc:
        return
    memory_cache.set(_CACHE_KEY_RATE, doc.get("rate"))
    memory_cache.set(_CACHE_KEY_UPDATED_AT, doc.get("updatedAt"))
    memory_cache.set(_CACHE_KEY_SOURCE, doc.get("source"))
    memory_cache.set(_CACHE_KEY_FIXED_BY, doc.get("fixedBy"))
