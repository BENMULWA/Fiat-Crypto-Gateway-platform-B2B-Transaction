"""What the IMM actually holds, read live. Nothing here comes from the ledger or from a default.

Sources, each fetched on its own so one failure never zeroes the rest (a failed source is reported, not guessed):
  * the ImpalaPay reseller float (real airtime bought by every top-up),
  * every Celo wallet we care about: USDT, USDC, cUSD, IMC and CELO for gas,
  * our liquidity in the Comet pools, from the on-chain LP tokens (share x reserves),
  * the Cardano USDA vault and the Stellar treasury,
  * the Mam-laka gateway balances, kept OUT of the real total whenever the gateway is a sandbox.

Valuation convention (same as the console): dollar assets and IMC count at face ($1), shilling amounts are converted by
the console at the base rate, CELO and XLM are shown but not valued.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

TOKENS = ("USDT", "USDC", "cUSD", "IMC")
STABLES = ("USDT", "USDC", "cUSD")
DECIMALS = {"cUSD": 18}
FETCH_TIMEOUT = 25


@dataclass
class Deps:
    wallets: list[dict]                                              # [{role, label, address, always}]
    contracts: dict[str, str]                                        # symbol -> token contract
    token_balance_fn: Callable[[str, str], float]
    celo_balance_fn: Callable[[str], float]
    pools_fn: Callable[[], dict]                                     # Comet's pool listing
    lp_fn: Callable[[str, str], tuple[float, float]]                 # (pair, wallet) -> (lp balance, lp total supply)
    lp_wallet: Optional[str]
    float_fn: Callable[[], Optional[float]]                          # ImpalaPay float in KES
    gateway_fn: Callable[[], dict]                                   # {"status", "data"} from the Mam-laka gateway
    gateway_is_sandbox: bool
    usda_fn: Optional[Callable[[], Optional[float]]] = None
    xlm_fn: Optional[Callable[[], Optional[float]]] = None
    extra: dict = field(default_factory=dict)


def _line(key: str, label: str, group: str, unit: str, balance: float, *, usd: Optional[float] = None, kes: bool = False,
          source: str = "live", address: Optional[str] = None, detail: Optional[dict] = None) -> dict:
    return {"key": key, "label": label, "group": group, "unit": unit, "balance": balance, "usd": usd, "kes": kes,
            "source": source, "address": address, "detail": detail}


async def _call(errors: list, source: str, fn: Callable, *args):
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=FETCH_TIMEOUT)
    except Exception as e:                                           # noqa: BLE001 - reported, never swallowed silently
        errors.append({"source": source, "message": str(e)[:160]})
        return None


async def gather(d: Deps) -> dict:
    lines: list[dict] = []
    errors: list[dict] = []

    # ---- 1. the airtime float we actually bought
    kes = await _call(errors, "ImpalaPay airtime float", d.float_fn)
    if kes is not None:
        lines.append(_line("AIRTIME_FLOAT", "Airtime float (ImpalaPay)", "airtime", "KES face", float(kes), kes=True))

    # ---- 2. Celo wallets: one task per wallet and token, run together
    async def wallet_tokens(w: dict):
        got = {}
        results = await asyncio.gather(*[_call(errors, f"{w['label']} {sym}", d.token_balance_fn, sym, w["address"]) for sym in TOKENS],
                                       _call(errors, f"{w['label']} CELO", d.celo_balance_fn, w["address"]))
        for sym, val in zip(TOKENS, results[:-1]):
            got[sym] = val
        got["CELO"] = results[-1]
        return w, got

    for w, got in await asyncio.gather(*[wallet_tokens(w) for w in d.wallets if w.get("address")]):
        for sym, val in got.items():
            if val is None or (val <= 0 and not w.get("always")):
                continue
            if sym == "CELO":
                lines.append(_line(f"{w['role']}:CELO", f"{w['label']}: CELO for gas", "other", "CELO", val, address=w["address"]))
            else:
                grp = "imc" if sym == "IMC" else "stable"
                lines.append(_line(f"{w['role']}:{sym}", f"{w['label']}: {sym}", grp, sym, val, usd=val, address=w["address"]))

    # ---- 3. our liquidity in the pools: share of each pool = our LP tokens / total LP supply
    if d.lp_wallet:
        listing = await _call(errors, "Comet pools", d.pools_fn)
        rows = []
        data = listing.get("data") if isinstance(listing, dict) else None
        rows = data if isinstance(data, list) else (data or {}).get("pools", []) if isinstance(data, dict) else []
        by_addr = {a.lower(): s for s, a in d.contracts.items()}
        for p in rows:
            a, b = by_addr.get(str(p.get("token0", "")).lower()), by_addr.get(str(p.get("token1", "")).lower())
            if not a or not b or not (int(p["reserve0"]) and int(p["reserve1"])):
                continue
            lp = await _call(errors, f"LP tokens {a}/{b}", d.lp_fn, p["pair"], d.lp_wallet)
            if not lp or lp[1] <= 0 or lp[0] <= 0:
                continue
            share = lp[0] / lp[1]
            amt = {a: int(p["reserve0"]) / 10 ** DECIMALS.get(a, 6) * share, b: int(p["reserve1"]) / 10 ** DECIMALS.get(b, 6) * share}
            face = sum(v for s, v in amt.items() if s in STABLES or s == "IMC")
            imc = amt.get("IMC")
            stable_sym = next((s for s in (a, b) if s != "IMC"), None)
            price = (amt[stable_sym] / imc) if (imc and stable_sym) else None
            lines.append(_line(f"POOL:{a}/{b}", f"Pool {a}/{b} (your liquidity)", "pool", f"{a} / {b}", share, usd=face,
                               address=d.lp_wallet,
                               detail={"pair": p["pair"], "share": share, "amounts": amt, "price": price,
                                       "reserves": {a: int(p["reserve0"]) / 10 ** DECIMALS.get(a, 6), b: int(p["reserve1"]) / 10 ** DECIMALS.get(b, 6)}}))

    # ---- 4. other real vaults
    if d.usda_fn:
        v = await _call(errors, "Cardano USDA vault", d.usda_fn)
        if v is not None and v > 0:
            lines.append(_line("N7_USDA", "Cardano USDA vault", "stable", "USDA", float(v), usd=float(v)))
    if d.xlm_fn:
        v = await _call(errors, "Stellar treasury", d.xlm_fn)
        if v is not None and v > 0:
            lines.append(_line("N9_XLM", "Stellar XLM", "other", "XLM", float(v)))

    # ---- 5. the Mam-laka gateway: real only if it is not a sandbox
    gw = await _call(errors, "Mam-laka gateway", d.gateway_fn)
    if isinstance(gw, dict) and gw.get("status") == "success":
        g = gw.get("data") or {}
        src = "sandbox" if d.gateway_is_sandbox else "live"
        grp = lambda real: "sandbox" if d.gateway_is_sandbox else real           # noqa: E731
        for key, label, real_grp, unit, k in (("kesBalance", "Paybill M-Pesa float (Mam-laka)", "mobile", "KES", True),
                                              ("artmBalance", "Airtel airtime (Mam-laka)", "airtime", "KES face", True),
                                              ("impaBalance", "IMP token (Mam-laka)", "imc", "IMC", False)):
            if g.get(key) is not None:
                v = float(g[key])
                lines.append(_line(f"GATEWAY:{key}", label, grp(real_grp), unit, v, usd=None if k else v, kes=k, source=src))
    elif isinstance(gw, dict):
        errors.append({"source": "Mam-laka gateway", "message": str(gw.get("message"))[:160]})

    return {"lines": lines, "errors": errors, "gatewaySandbox": d.gateway_is_sandbox}


def strip_addresses(result: dict) -> dict:
    """What a non-operator may see: the same figures, without wallet addresses."""
    out = {**result, "lines": []}
    for ln in result["lines"]:
        c = dict(ln)
        c["address"] = None
        if c.get("detail"):
            c["detail"] = {k: v for k, v in c["detail"].items() if k != "pair"}
        out["lines"].append(c)
    return out
