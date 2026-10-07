"""Bring every IMC pool back to $1 per IMC, with real funds, from the engine's Comet wallet.

Why this exists: a pool that prices IMC away from $1 is drained by arbitrage within minutes (it happened on
2026-10-07: two contracts bought IMC cheap in USDT/IMC and sold it into IMC/USDC), and a corridor run that
sells into a skewed pool would lose most of its value. The fix is one swap per pool in the opposite direction.
The pools are fixed together: fixing one alone just lets a bot move value from it into the other.
Comet's docs describe swaps only through the API, so the swaps go through that API, from the same wallet the
corridor's pool exit uses.

Safety, in order:
  * preview() never moves anything; execute() re-plans from live reserves and refuses if anything changed;
  * it refuses while a corridor run is open (they share the engine wallet) and above a hard spend cap;
  * everything is funded up front, every quote must match its plan, and only then do the swaps go out back to back;
  * each swap's real output is read back from Celo; every attempt is written to `pool_rebalances`.
"""
from __future__ import annotations

import asyncio
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

FEE = 0.003                    # Uniswap V2 swap fee, kept in the pool
BALANCED_WITHIN = 0.005        # a price within 0.5% of $1 needs no rebalance
DUST = 0.05                    # a pool with less than this on a side is not worth a bot's time
MAX_SPEND = 5.0                # hard cap on what one swap may spend, in tokens
QUOTE_TOLERANCE = 0.03         # the live quote may differ from the plan by up to 3%
DECIMALS = 6
STABLES = ("USDC", "USDT")
AUDIT_COLLECTION = "pool_rebalances"

_lock = asyncio.Lock()
_jobs: dict[str, dict] = {}        # job id -> state, newest last; progress for the console
_tasks: set = set()               # strong references so a running job is never garbage collected
_current: Optional[str] = None    # the job that is running right now, set synchronously by start()


@dataclass
class Deps:
    comet: Any
    contracts: dict[str, str]                                  # symbol -> token contract address
    balance_fn: Callable[[str, str], float]                    # (symbol, address) -> on-chain balance
    fund_fn: Callable[[str, str, float], Any]                  # async (symbol, address, amount) -> tx hash
    receipt_fn: Callable[[str, str], Optional[dict]]           # (tx hash, out symbol) -> {"amount", "pair"} | None
    user_id: str
    treasury_address: str
    db: Any = None
    poll_attempts: int = 20
    poll_seconds: float = 3.0
    max_spend: float = MAX_SPEND
    extra: dict = field(default_factory=dict)


class RebalanceError(Exception):
    """A refusal or failure with a message the console can show as it is."""


# ---------------------------------------------------------------------------------------------- pure maths
def round_up(x: float) -> float:
    """x rounded UP to a whole base unit (6 decimals), so that what is sent covers what is needed."""
    return math.ceil(round(x * 10 ** DECIMALS, 4)) / 10 ** DECIMALS


def sendable(x: float) -> float:
    """The amount to hand to the transfer code. It truncates amount * 1e6 to an integer, so a float like
    0.676422 (really 676421.9999...) would send one unit short; the nudge keeps it on the right unit."""
    return round_up(x) + 1e-9


def _out(amount_in: float, r_in: float, r_out: float) -> float:
    ai = amount_in * (1 - FEE)
    return ai * r_out / (r_in + ai)


def read_pools(listing: dict, contracts: dict[str, str]) -> list[dict]:
    """Comet's pool listing as [{pair, a, b, ra, rb}] with symbols and float reserves, known tokens only."""
    by_addr = {addr.lower(): sym for sym, addr in contracts.items()}
    data = listing.get("data") if isinstance(listing, dict) else None
    rows = data if isinstance(data, list) else (data or {}).get("pools", []) if isinstance(data, dict) else []
    pools = []
    for p in rows:
        a, b = by_addr.get(str(p["token0"]).lower()), by_addr.get(str(p["token1"]).lower())
        if not a or not b:
            continue
        pools.append({"pair": p.get("pair"), "a": a, "b": b,
                      "ra": int(p["reserve0"]) / 10 ** DECIMALS, "rb": int(p["reserve1"]) / 10 ** DECIMALS})
    return pools


def reserves(pool: dict, symbol: str) -> float:
    return pool["ra"] if pool["a"] == symbol else pool["rb"]


def _fix(pool: dict, stable: str) -> Optional[dict]:
    """The single swap that returns an IMC/<stable> pool to equal reserves ($1 per IMC), or None if already there."""
    imc_r, st_r = reserves(pool, "IMC"), reserves(pool, stable)
    price = st_r / imc_r
    if abs(1 - price) <= BALANCED_WITHIN:
        return None
    target = math.sqrt(imc_r * st_r)                             # equal reserves
    if price < 1:                                                # IMC too plentiful: buy IMC with the stable
        spend = round_up((target - st_r) / (1 - FEE))
        got = _out(spend, st_r, imc_r)
        spend_sym, recv_sym, after = stable, "IMC", (st_r + spend) / (imc_r - got)
    else:                                                        # IMC too scarce: sell IMC for the stable
        spend = round_up((target - imc_r) / (1 - FEE))
        got = _out(spend, imc_r, st_r)
        spend_sym, recv_sym, after = "IMC", stable, (st_r - got) / (imc_r + spend)
    return {"pair": f"IMC/{stable}", "pairAddress": pool["pair"], "price": price, "spend": spend_sym, "receive": recv_sym,
            "spendAmount": spend, "receiveAmount": got, "priceAfter": after,
            "imcReserve": imc_r, "stableReserve": st_r}


def plan(pools: list[dict], max_spend: float = MAX_SPEND) -> dict:
    """The swaps that would bring every IMC pool back to $1, and what stands in the way. Read-only maths."""
    out: dict[str, Any] = {"pair": "IMC/USDC", "seeded": False, "imcReserve": 0.0, "usdcReserve": 0.0, "price": None,
                           "balanced": False, "actions": [], "blockers": [], "otherPools": []}
    main = next((p for p in pools if {p["a"], p["b"]} == {"IMC", "USDC"}), None)
    if main:
        imc_r, usdc_r = reserves(main, "IMC"), reserves(main, "USDC")
        out.update(imcReserve=imc_r, usdcReserve=usdc_r, seeded=bool(imc_r and usdc_r))
    if not out["seeded"]:
        out["blockers"].append("The IMC/USDC pool is empty. Add liquidity in the Comet dashboard first.")
        return out
    out["price"] = out["usdcReserve"] / out["imcReserve"]

    actions = []
    for p in pools:                                              # other IMC pools first, the main one last
        if p is main or "IMC" not in (p["a"], p["b"]):
            continue
        other = p["b"] if p["a"] == "IMC" else p["a"]
        if other not in STABLES:
            continue
        r_imc, r_other = reserves(p, "IMC"), reserves(p, other)
        out["otherPools"].append({"pair": f"IMC/{other}", "imcReserve": r_imc, "otherReserve": r_other,
                                  "price": (r_other / r_imc) if r_imc else None})
        if r_imc >= DUST and r_other >= DUST:
            fix = _fix(p, other)
            if fix:
                actions.append(fix)
    main_fix = _fix(main, "USDC")
    if main_fix:
        actions.append(main_fix)
    out["actions"] = actions
    out["balanced"] = not actions
    for a in actions:
        if a["spendAmount"] > max_spend:
            out["blockers"].append(f"{a['pair']} needs {a['spendAmount']:.4f} {a['spend']}, over the {max_spend:g} cap for one swap.")
    return out


# ---------------------------------------------------------------------------------------------- orchestration
async def _live_run(db) -> Optional[str]:
    if db is None:
        return None
    from workers.corridor_worker import RUNS_COLLECTION, TERMINAL_STATES
    doc = await db[RUNS_COLLECTION].find_one({"status": {"$nin": list(TERMINAL_STATES)}, "cancelledAt": {"$exists": False}}, {"_id": 1})
    return (doc or {}).get("_id")


async def _snapshot(d: Deps) -> tuple[dict, str]:
    listing = await asyncio.to_thread(d.comet.list_amm_pools)
    pl = plan(read_pools(listing, d.contracts), d.max_spend)
    wallet = await asyncio.to_thread(d.comet.get_or_create_wallet, str(d.user_id))
    address = (wallet.get("data") or {}).get("address")
    if wallet.get("status") != "success" or not address:
        raise RebalanceError(f"Could not find the engine's Comet wallet: {wallet.get('message') or 'no address'}")
    return pl, address


def _needs(actions: list[dict]) -> dict[str, float]:
    need: dict[str, float] = {}
    for a in actions:
        need[a["spend"]] = need.get(a["spend"], 0.0) + a["spendAmount"]
    return need


async def preview(d: Deps) -> dict:
    """Everything the console shows before anyone presses the button. Nothing moves."""
    pl, address = await _snapshot(d)
    res: dict[str, Any] = {"plan": pl, "engineWallet": {"address": address}, "liveRun": await _live_run(d.db),
                           "canExecute": False, "reasons": list(pl["blockers"]), "needs": []}
    for sym, amount in _needs(pl["actions"]).items():
        held = await asyncio.to_thread(d.balance_fn, sym, address)
        treasury = await asyncio.to_thread(d.balance_fn, sym, d.treasury_address)
        shortfall = max(amount - held, 0.0)
        shortfall = 0.0 if shortfall < 0.5 / 10 ** DECIMALS else shortfall      # within half a unit: nothing to send
        res["needs"].append({"symbol": sym, "amount": amount, "held": held, "treasury": treasury, "shortfall": shortfall,
                             "missing": max(shortfall - treasury, 0.0)})
        if shortfall > treasury + 1e-9:
            res["reasons"].append(f"The swaps need {amount:.4f} {sym}; the engine wallet holds {held:.4f} and the treasury can add only "
                                  f"{treasury:.4f}. Send about {shortfall - treasury:.2f} more {sym} to the treasury wallet.")
    if res["liveRun"]:
        res["reasons"].append(f"Corridor run {res['liveRun']} is open and uses the same wallet. Wait for it to finish or cancel it.")
    if pl["balanced"] and not pl["blockers"]:
        res["reasons"].append("Every IMC pool is already within 0.5% of $1. Nothing to do.")
    res["canExecute"] = bool(pl["actions"]) and not res["reasons"]
    return res


async def execute(d: Deps, requested_by: str, on_step: Optional[Callable[[str], None]] = None) -> dict:
    """Fixes every skewed IMC pool. Re-plans from live reserves first; raises RebalanceError with a plain message on any refusal.
    `on_step` receives a plain sentence at each milestone so the console can show progress."""
    step = on_step or (lambda msg: None)
    if _lock.locked():
        raise RebalanceError("A rebalance is already in progress.")
    async with _lock:
        step("Checking the pools and balances")
        pv = await preview(d)
        if not pv["canExecute"]:
            raise RebalanceError(pv["reasons"][0] if pv["reasons"] else "Nothing to rebalance.")
        actions, address = pv["plan"]["actions"], pv["engineWallet"]["address"]
        audit: dict[str, Any] = {"at": datetime.utcnow(), "by": requested_by, "pricesBefore": {a["pair"]: a["price"] for a in actions},
                                 "planned": [{k: a[k] for k in ("pair", "spend", "spendAmount", "receive", "receiveAmount")} for a in actions],
                                 "fundTxs": [], "swaps": []}

        async def stop(msg: str) -> None:
            audit["error"] = msg
            await _save(d, audit)
            raise RebalanceError(msg)

        # 1. fund everything up front, so the swaps can go out back to back
        for need in pv["needs"]:
            sym, amount, held = need["symbol"], need["amount"], need["held"]
            if need["shortfall"] <= 1e-9:
                continue
            step(f"Sending {need['shortfall']:.6f} {sym} from the treasury to the engine wallet")
            try:
                fund_tx = await d.fund_fn(sym, address, sendable(need["shortfall"]))
            except Exception as e:
                await stop(f"Could not move {need['shortfall']:.6f} {sym} from the treasury to the engine wallet: {e}")
            audit["fundTxs"].append(fund_tx)
            step(f"Waiting for the {sym} to show on Celo")
            for _ in range(d.poll_attempts):
                await asyncio.sleep(d.poll_seconds)
                try:
                    held = await asyncio.to_thread(d.balance_fn, sym, address)
                except Exception:
                    continue
                if held + 1e-7 >= amount:
                    break
            else:
                await stop(f"Sent {need['shortfall']:.6f} {sym} (tx {fund_tx}) but it is not visible in the engine wallet yet. "
                           "It is not lost; try again once it shows on Celo.")

        # 2. every live quote must match its plan before anything is swapped
        step("Checking each quote against the plan")
        for a in actions:
            try:
                q = await asyncio.to_thread(d.comet.get_amm_quote, a["spend"], a["receive"], d.comet.to_base_units(a["spendAmount"]))
            except Exception as e:
                await stop(f"Could not quote {a['pair']}: {e}")
            if q.get("status") != "success":
                await stop(f"The {a['pair']} pool gave no quote: {q.get('message')}")
            quoted = d.comet.from_base_units(str(q.get("data", {}).get("amountOut", 0)))
            if quoted <= 0 or abs(quoted / a["receiveAmount"] - 1) > QUOTE_TOLERANCE:
                await stop(f"The live quote for {a['pair']} ({quoted:.6f} {a['receive']}) does not match the plan "
                           f"({a['receiveAmount']:.6f}). The pool moved; nothing was swapped.")
            a["quotedOut"] = quoted

        # 3. the swaps, back to back
        for i, a in enumerate(actions):
            step(f"Swapping {a['spendAmount']:.6f} {a['spend']} for IMC in the {a['pair']} pool")
            done = f" The earlier swap ({', '.join(s['pair'] for s in audit['swaps'])}) already went through." if audit["swaps"] else ""
            try:
                res = await asyncio.to_thread(d.comet.execute_amm_swap, external_user_id=str(d.user_id), from_symbol=a["spend"],
                                              to_symbol=a["receive"], amount_in=d.comet.to_base_units(a["spendAmount"]))
            except Exception as e:
                await stop(f"The {a['pair']} swap call failed. The {a['spend']} is in the engine wallet, not lost: {e}.{done}")
            if res.get("status") != "success":
                await stop(f"Comet rejected the {a['pair']} swap. The {a['spend']} is in the engine wallet, not lost: {res.get('message')}.{done}")
            audit["swaps"].append({"pair": a["pair"], "txHash": (res.get("data") or {}).get("txHash"), "spent": a["spendAmount"],
                                   "spentSymbol": a["spend"], "receivedSymbol": a["receive"], "quotedOut": a["quotedOut"]})

        # 4. what the pools really paid, read from Celo (falls back to the quote and says so)
        step("Reading the real amounts from Celo")
        for s in audit["swaps"]:
            confirmed = None
            if s["txHash"]:
                try:
                    confirmed = await asyncio.to_thread(d.receipt_fn, s["txHash"], s["receivedSymbol"])
                except Exception:
                    confirmed = None
            s["received"] = confirmed["amount"] if confirmed else s["quotedOut"]
            s["confirmed"] = bool(confirmed)
            s["priceBefore"] = audit["pricesBefore"][s["pair"]]

        # 5. the pools afterwards
        after = None
        try:
            after = plan(read_pools(await asyncio.to_thread(d.comet.list_amm_pools), d.contracts), d.max_spend)
        except Exception:
            pass
        audit["pricesAfter"] = ({"IMC/USDC": after["price"], **{o["pair"]: o["price"] for o in after["otherPools"]}} if after else None)
        await _save(d, audit)
        return {"ok": True, "swaps": audit["swaps"], "fundTxs": audit["fundTxs"], "engineWallet": address,
                "pricesAfter": audit["pricesAfter"]}


def _snapshot_job(job: dict) -> dict:
    return {k: (list(v) if k == "steps" else v) for k, v in job.items()}


async def start(d: Deps, requested_by: str) -> dict:
    """Starts a rebalance in the background and returns at once with a job to follow. The work takes a minute or
    more (it waits on Celo between steps), longer than a browser request should be kept open."""
    global _current
    if _current is not None or _lock.locked():
        raise RebalanceError("A rebalance is already in progress.")
    pv = await preview(d)                                        # refuse straight away, with the reason, when it cannot run
    if not pv["canExecute"]:
        raise RebalanceError(pv["reasons"][0] if pv["reasons"] else "Nothing to rebalance.")
    job = {"id": uuid.uuid4().hex[:12], "status": "running", "startedAt": datetime.utcnow().isoformat() + "Z",
           "steps": [], "result": None, "error": None, "by": requested_by}
    _jobs[job["id"]] = job
    while len(_jobs) > 20:
        _jobs.pop(next(iter(_jobs)))
    _current = job["id"]

    def note(msg: str) -> None:
        job["steps"].append({"at": datetime.utcnow().isoformat() + "Z", "msg": msg})

    async def run() -> None:
        global _current
        try:
            job["result"] = await execute(d, requested_by, on_step=note)
            job["status"] = "done"
            note("Done")
        except RebalanceError as e:
            job["status"], job["error"] = "failed", str(e)
        except Exception as e:                                   # never leave a job "running" forever
            job["status"], job["error"] = "failed", f"Unexpected error: {e}"
        finally:
            job["finishedAt"] = datetime.utcnow().isoformat() + "Z"
            _current = None

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return _snapshot_job(job)


def latest_job() -> Optional[dict]:
    """The newest job this server process has seen (the audit log in the database keeps the permanent record)."""
    return _snapshot_job(_jobs[next(reversed(_jobs))]) if _jobs else None


async def _save(d: Deps, audit: dict) -> None:
    if d.db is None:
        return
    try:
        await d.db[AUDIT_COLLECTION].insert_one(dict(audit))
    except Exception:
        pass
