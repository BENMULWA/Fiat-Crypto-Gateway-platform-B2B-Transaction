"""Live market rates for dealer quoting.

Rates are expressed the same way as the treasury rate book's `usd_base_rates`:
units of each currency per 1 USD, with USDT expressed per USD too (so a USDT ->
KES market rate is rates["KES"] / rates["USDT"]). That lets a live snapshot
overlay the rate book without touching any pricing maths.

Providers (tried in order, first success wins):
  1. OANDA Exchange Rates  -- needs OANDA_API_KEY (bid/ask/midpoint, intraday)
  2. Open Exchange Rates   -- needs OPENEXCHANGERATES_APP_ID (hourly on the free plan)
  3. ExchangeRate-API open -- no key, FX mid, updated ONCE A DAY by the provider
USDT's own price vs USD comes from CoinGecko (no key needed) when reachable, so a
dealer selling USDT sees USDT's real market price, not just the FX mid.

Nothing here falls back to a hardcoded number: if every provider fails or the
cached snapshot is too old, callers get a clear error and the dealer must
choose the rate book explicitly.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

CACHE_TTL_SECONDS = int(os.getenv("FX_CACHE_TTL_SECONDS", "60"))
# How long a fetched snapshot may be used when providers are unreachable.
MAX_SNAPSHOT_AGE_SECONDS = int(os.getenv("FX_MAX_AGE_SECONDS", "900"))
_HTTP_TIMEOUT = 8.0

_cache: dict[str, Any] = {"snapshot": None, "fetched_monotonic": 0.0}


class LiveRateUnavailable(Exception):
    pass


# Neither feed lists these USD-pegged tokens; priced 1:1 with USD (USDT itself has
# its own market price from CoinGecko, handled in _fetch).
PARITY_ASSETS = {"USD", "USDC", "USDA", "CUSD", "IMC", "IMP"}


class NotConfigured(LiveRateUnavailable):
    """Provider has no key set -- skipped silently, not an error."""


async def _from_oanda(client: httpx.AsyncClient) -> dict[str, Any]:
    """OANDA Exchange Rates API v2 /rates/spot (spec: https://api.corporatefxservices.com/openapi.json).
    Every quote currency returned counts against the plan's quote limit, so only the
    corridors we actually quote are requested (OANDA_QUOTES), never the default ~200."""
    key = os.getenv("OANDA_API_KEY", "").strip()
    if not key:
        raise NotConfigured("OANDA_API_KEY not set")
    wanted = [c.strip().upper() for c in os.getenv("OANDA_QUOTES", "KES,UGX,NGN").split(",") if c.strip()]
    r = await client.get(
        os.getenv("OANDA_BASE_URL", "https://api.corporatefxservices.com/v2/rates/spot.json"),
        params=[("base", "USD")] + [("quote", c) for c in wanted],
        headers={"Authorization": f"Bearer {key}"},
    )
    if r.status_code in (400, 401, 403):
        try:
            detail = r.json().get("message") or ""
        except Exception:
            detail = ""
        raise LiveRateUnavailable(f"OANDA rejected the request ({detail or r.status_code}) -- check OANDA_API_KEY")
    if r.status_code == 429:
        raise LiveRateUnavailable("OANDA quote limit reached")
    r.raise_for_status()
    data = r.json()
    rates: dict[str, float] = {}
    for q in data.get("quotes") or []:
        try:
            mid = q.get("midpoint")
            value = float(mid) if mid is not None else (float(q["bid"]) + float(q["ask"])) / 2
        except (KeyError, TypeError, ValueError):
            continue
        if q.get("quote_currency"):
            rates[str(q["quote_currency"]).upper()] = value
    if not rates:
        raise LiveRateUnavailable("OANDA returned no usable quotes")
    skipped = (data.get("meta") or {}).get("skipped_currency_pairs") or []
    return {
        "provider": "OANDA", "cadence": "spot, 5-second granularity", "rates": rates,
        "providerUpdatedAt": (data.get("meta") or {}).get("request_time"), "skipped": skipped,
        "remaining": r.headers.get("X-Rate-Limit-Remaining"),
    }


async def _from_open_exchange_rates(client: httpx.AsyncClient) -> dict[str, Any]:
    app_id = os.getenv("OPENEXCHANGERATES_APP_ID", "").strip()
    if not app_id:
        raise NotConfigured("OPENEXCHANGERATES_APP_ID not set")
    r = await client.get("https://openexchangerates.org/api/latest.json", params={"app_id": app_id})
    r.raise_for_status()
    data = r.json()
    ts = data.get("timestamp")
    return {
        "provider": "Open Exchange Rates",
        "cadence": "hourly (plan dependent)",
        "rates": {k.upper(): float(v) for k, v in (data.get("rates") or {}).items()},
        "providerUpdatedAt": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None,
    }


async def _from_exchangerate_api_open(client: httpx.AsyncClient) -> dict[str, Any]:
    r = await client.get("https://open.er-api.com/v6/latest/USD")
    r.raise_for_status()
    data = r.json()
    if data.get("result") != "success":
        raise LiveRateUnavailable("ExchangeRate-API returned a non-success result")
    ts = data.get("time_last_update_unix")
    return {
        "provider": "ExchangeRate-API (open)",
        "cadence": "daily mid-market rate",
        "rates": {k.upper(): float(v) for k, v in (data.get("rates") or {}).items()},
        "providerUpdatedAt": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None,
    }


async def _usdt_usd(client: httpx.AsyncClient) -> float | None:
    """USDT's market price in USD (e.g. 0.9996). Optional -- None if unreachable."""
    try:
        headers = {}
        key = os.getenv("COINGECKO_API_KEY", "").strip()
        if key:
            headers["x-cg-demo-api-key"] = key
        r = await client.get("https://api.coingecko.com/api/v3/simple/price",
                             params={"ids": "tether", "vs_currencies": "usd"}, headers=headers)
        r.raise_for_status()
        price = float(r.json()["tether"]["usd"])
        return price if 0.9 < price < 1.1 else None  # ignore obviously broken prints
    except Exception:
        return None


async def _fetch() -> dict[str, Any]:
    errors: list[str] = []
    oanda = base = None
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        try:
            oanda = await _from_oanda(client)
        except NotConfigured:
            pass
        except Exception as exc:
            errors.append(f"OANDA: {exc}")
        for provider in (_from_open_exchange_rates, _from_exchangerate_api_open):
            try:
                base = await provider(client)
                break
            except NotConfigured:
                continue
            except Exception as exc:
                errors.append(f"{provider.__name__.replace('_from_', '')}: {exc}")
        if not oanda and not base:
            raise LiveRateUnavailable("; ".join(errors) or "no provider available")
        usdt = await _usdt_usd(client)

    rates: dict[str, float] = {}
    sources: dict[str, str] = {}
    if base:
        rates.update(base["rates"])
        sources.update({k: base["provider"] for k in base["rates"]})
    if oanda:
        rates.update(oanda["rates"])
        sources.update({k: "OANDA" for k in oanda["rates"]})
    rates["USD"] = 1.0
    # USDT per USD, so USDT->KES = rates[KES]/rates[USDT] = KES-per-USD x usdt_price
    rates["USDT"] = (1.0 / usdt) if usdt else 1.0
    lead = oanda or base
    return {
        "rates": rates,
        "sources": sources,
        "provider": ("OANDA + " + base["provider"]) if (oanda and base) else lead["provider"],
        "cadence": lead["cadence"],
        "providerUpdatedAt": lead["providerUpdatedAt"],
        "usdtUsd": usdt,
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "providerErrors": errors,
        "oandaSkipped": (oanda or {}).get("skipped") or [],
    }


async def get_live_snapshot(db=None, force: bool = False) -> dict[str, Any]:
    """Cached snapshot (TTL FX_CACHE_TTL_SECONDS). `force` bypasses the cache."""
    age = time.monotonic() - _cache["fetched_monotonic"]
    if not force and _cache["snapshot"] and age < CACHE_TTL_SECONDS:
        return {**_cache["snapshot"], "ageSeconds": round(age)}
    try:
        snap = await _fetch()
    except Exception as exc:
        # Providers down: reuse the last good snapshot only while it is still young enough.
        if _cache["snapshot"] and age <= MAX_SNAPSHOT_AGE_SECONDS:
            return {**_cache["snapshot"], "ageSeconds": round(age), "degraded": str(exc)}
        raise LiveRateUnavailable(f"Live market rates unavailable ({exc})") from exc
    _cache["snapshot"] = snap
    _cache["fetched_monotonic"] = time.monotonic()
    if db is not None:
        try:
            await db["fx_market_rates"].update_one(
                {"_id": "latest"},
                {"$set": {k: v for k, v in snap.items() if k != "rates"} | {"sample": {a: snap["rates"].get(a) for a in ("KES", "UGX", "NGN", "USDT")}}},
                upsert=True,
            )
        except Exception:
            pass
    return {**snap, "ageSeconds": 0}


def market_rate(snapshot: dict[str, Any], from_asset: str, to_asset: str) -> float | None:
    r = snapshot["rates"]
    f, t = r.get(from_asset.upper()), r.get(to_asset.upper())
    return (t / f) if f and t else None


async def apply_live_rates(db, rate_book: dict, assets: list[str]) -> tuple[dict, dict]:
    """Returns (rate_book copy with live rates overlaid, info). Raises
    LiveRateUnavailable when the feed is down or a requested asset has no live rate."""
    snap = await get_live_snapshot(db)
    overlay = dict(rate_book)
    rates = dict(rate_book.get("usd_base_rates", {}))
    for asset in {a.upper() for a in assets} | {"USDT"}:
        live = snap["rates"].get(asset)
        if live is None and asset in PARITY_ASSETS:
            live = 1.0
        if live is None:
            if asset == "USDT":
                continue
            raise LiveRateUnavailable(f"No live market rate for {asset}")
        rates[asset] = live
    overlay["usd_base_rates"] = rates
    return overlay, {
        "priceSource": "live",
        "marketProvider": snap["provider"],
        "marketCadence": snap["cadence"],
        "marketProviderUpdatedAt": snap["providerUpdatedAt"],
        "marketFetchedAt": snap["fetchedAt"],
        "usdtUsd": snap["usdtUsd"],
    }


# --- CBK reference via the Comet engine ------------------------------------
# Comet publishes the Central Bank of Kenya's daily indicative rates (it
# re-checks CBK every ~15 minutes) at GET /api/v1/rates. CBK quotes KES per
# unit of each currency, so rates are converted to the same "units per USD"
# shape as everything above. CBK has no NGN, and treats USDT/USDC at 1 USD.

CBK_CACHE_TTL_SECONDS = int(os.getenv("CBK_CACHE_TTL_SECONDS", "900"))
_cbk_cache: dict[str, Any] = {"snapshot": None, "fetched_monotonic": 0.0}


def _fetch_cbk_sync() -> dict[str, Any]:
    from services.comet_client import CometClient
    res = CometClient()._get("/api/v1/rates")
    if res.get("status") != "success":
        raise LiveRateUnavailable(f"Comet CBK rates unavailable ({str(res.get('message'))[:120]})")
    d = res["data"]
    usd_kes = float((d.get("usdKes") or {}).get("mid") or 0)
    if usd_kes <= 0:
        raise LiveRateUnavailable("Comet returned no USD/KES rate")
    rates = {"KES": usd_kes, "USD": 1.0, "USDT": 1.0, "USDC": 1.0}
    for cur, v in (d.get("fiat") or {}).items():
        kes_per_unit = float((v or {}).get("kesPerUnit") or 0)
        if kes_per_unit > 0:
            rates[cur.upper()] = usd_kes / kes_per_unit  # units of `cur` per 1 USD
    return {
        "rates": rates,
        "provider": "Central Bank of Kenya (via Comet)",
        "cadence": "daily indicative rate, re-checked every 15 min",
        "cbkDate": d.get("date"),
        "providerUpdatedAt": d.get("fetchedAt"),
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "usdKes": usd_kes,
    }


async def get_cbk_snapshot(force: bool = False) -> dict[str, Any]:
    import asyncio
    age = time.monotonic() - _cbk_cache["fetched_monotonic"]
    if not force and _cbk_cache["snapshot"] and age < CBK_CACHE_TTL_SECONDS:
        return {**_cbk_cache["snapshot"], "ageSeconds": round(age)}
    try:
        snap = await asyncio.to_thread(_fetch_cbk_sync)
    except Exception as exc:
        if _cbk_cache["snapshot"] and age <= 3600:
            return {**_cbk_cache["snapshot"], "ageSeconds": round(age), "degraded": str(exc)}
        raise LiveRateUnavailable(f"CBK rates unavailable ({exc})") from exc
    _cbk_cache["snapshot"] = snap
    _cbk_cache["fetched_monotonic"] = time.monotonic()
    return {**snap, "ageSeconds": 0}


async def apply_cbk_rates(db, rate_book: dict, assets: list[str]) -> tuple[dict, dict]:
    snap = await get_cbk_snapshot()
    overlay = dict(rate_book)
    rates = dict(rate_book.get("usd_base_rates", {}))
    for asset in {a.upper() for a in assets} | {"USDT"}:
        v = snap["rates"].get(asset)
        if v is None and asset in PARITY_ASSETS:
            v = 1.0
        if v is None:
            raise LiveRateUnavailable(f"CBK publishes no rate for {asset}")
        rates[asset] = v
    overlay["usd_base_rates"] = rates
    return overlay, {
        "priceSource": "cbk",
        "marketProvider": snap["provider"],
        "marketCadence": snap["cadence"],
        "marketProviderUpdatedAt": snap["providerUpdatedAt"],
        "marketFetchedAt": snap["fetchedAt"],
        "cbkDate": snap.get("cbkDate"),
    }
