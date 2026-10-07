"""Mint IMC against a real airtime top-up through Comet's booking endpoints.

The operator tops up the ImpalaPay float (5% arrives as extra float), then pastes the M-Pesa / ImpalaPay receipt
of that top-up here. Comet verifies the receipt against the gateway, mints the IMC into the treasury wallet, and
(optionally) delivers it to another address. The receipt is Comet's idempotency key: it books exactly once.

What this does NOT do: it never retries a mint on its own, never reuses a receipt, and never reports a booking
as delivered until the IMC transfer is seen on Celo. Every attempt is written to `imc_bookings`.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

COLLECTION = "imc_bookings"
RECEIPT_RE = re.compile(r"^[A-Za-z0-9._\-]{4,128}$")
MAX_CASH_KES = 1_000_000
TOLERANCE = 0.01                 # a mint within 1% of Comet's own preview counts as matching

_jobs: dict[str, dict] = {}
_tasks: set = set()
_current: Optional[str] = None


class BookingError(Exception):
    """A refusal or failure with a message the console can show as it is."""


@dataclass
class Deps:
    comet: Any
    db: Any
    receipts: Callable[[Any], Any]           # db -> ImcReceipts (reads Celo, records transfers)
    treasury_address: str
    chain: str = "celo"
    poll_attempts: int = 20
    poll_seconds: float = 3.0


def clean_receipt(raw: str) -> str:
    r = (raw or "").strip()
    if not RECEIPT_RE.match(r):
        raise BookingError("The receipt must be 4 to 128 letters, digits, dots, dashes or underscores, exactly as it appears in the M-Pesa or ImpalaPay message.")
    return r


def clean_cash(raw: Any) -> Optional[float]:
    if raw in (None, ""):
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 < raw <= MAX_CASH_KES:
        raise BookingError(f"Cash must be a number between 1 and {MAX_CASH_KES:,} KES.")
    return float(raw)


def clean_address(raw: Optional[str]) -> Optional[str]:
    if not raw or not str(raw).strip():
        return None
    from web3 import Web3
    a = str(raw).strip()
    if not Web3.is_address(a):
        raise BookingError("The delivery address is not a valid Celo address.")
    return Web3.to_checksum_address(a)


def _parse(msg: Any) -> dict:
    """Comet's error body is JSON text; pull out what we can use (txHash, minted, the message)."""
    try:
        body = json.loads(msg) if isinstance(msg, str) else dict(msg)
        return body if isinstance(body, dict) else {"error": str(msg)}
    except Exception:
        return {"error": str(msg)}


def _undelivered(existing: Optional[dict]) -> bool:
    """A booking that minted (into Comet's treasury) but was never delivered to our wallet: it may be delivered once."""
    return bool(existing and existing.get("status") == "booked" and not existing.get("sendTxHash") and not existing.get("arrivedImc"))


NOTHING_TO_BOOK = ("nothing to book", "already issued")


async def _save(d: Deps, receipt: str, fields: dict) -> None:
    try:
        await d.db[COLLECTION].update_one({"_id": receipt}, {"$set": {**fields, "updatedAt": datetime.utcnow()},
                                                              "$setOnInsert": {"createdAt": datetime.utcnow()}}, upsert=True)
    except Exception:
        pass


async def list_bookings(d: Deps, limit: int = 12) -> list[dict]:
    docs = await d.db[COLLECTION].find({}).sort("updatedAt", -1).limit(limit).to_list(length=limit)
    out = []
    for x in docs:
        y = dict(x)
        y["receipt"] = y.pop("_id")
        for k in ("createdAt", "updatedAt"):
            if y.get(k) is not None:
                y[k] = y[k].isoformat() + "Z"
        out.append(y)
    return out


async def book(d: Deps, receipt: str, cash_kes: Optional[float], deliver_to: Optional[str], requested_by: str,
               on_step: Optional[Callable[[str], None]] = None) -> dict:
    """Books one receipt. Returns {receipt, txHash, imc, arrived, treasury, ...} or raises BookingError."""
    step = on_step or (lambda m: None)
    step("Checking the receipt")
    existing = await d.db[COLLECTION].find_one({"_id": receipt})
    if existing and existing.get("status") == "booked" and not (_undelivered(existing) and deliver_to):
        raise BookingError(f"Receipt {receipt} was already booked (transaction {existing.get('txHash')}). A receipt books once, ever.")
    if _undelivered(existing):
        step(f"This receipt already minted into Comet's treasury ({existing.get('treasury')}). Asking Comet to deliver it to your wallet")
    await _save(d, receipt, {"status": "pending" if not _undelivered(existing) else "booked", "cashKes": cash_kes, "deliverTo": deliver_to, "by": requested_by, "chain": d.chain})

    step("Asking Comet to verify the receipt and mint. This can take up to a minute while Celo confirms")
    try:
        if deliver_to:
            res = await asyncio.to_thread(d.comet.book_and_send, receipt, deliver_to, cash_kes, d.chain)
        else:
            res = await asyncio.to_thread(d.comet.book_imc, receipt, cash_kes, d.chain)
    except Exception as e:
        await _save(d, receipt, {"status": "failed", "error": str(e)})
        raise BookingError(f"The request to Comet failed before an answer came back: {e}. Do not assume nothing happened: "
                           "booking the same receipt again is safe, because Comet books each receipt once.")

    if res.get("status") != "success":
        body, code = _parse(res.get("message")), res.get("statusCode")
        text = body.get("error") or body.get("message") or str(res.get("message"))
        tx = body.get("txHash")
        if any(p in str(text).lower() for p in NOTHING_TO_BOOK):
            await _save(d, receipt, {"status": "nothing", "error": text})
            raise BookingError("Nothing to mint. Comet mints the airtime in the float that is not yet IMC, whichever receipt you give, and every KES in the float is already issued. "
                               f"Top up first, then book. (Comet said: {text})")
        if code == 409 and tx:                                                 # booked before: recover the original mint
            step("Comet says this receipt was already booked. Recovering the original transaction")
            data = {"txHash": tx, "recovered": True}
        elif code == 409:
            await _save(d, receipt, {"status": "pending", "error": text})
            raise BookingError(f"Comet says the mint is still confirming ({text}). Wait a minute and press the button again with the same receipt.")
        elif code == 502 and body.get("minted"):
            await _save(d, receipt, {"status": "pending", "txHash": tx, "minted": True, "error": text})
            raise BookingError(f"The IMC was minted (transaction {tx}) but delivering it failed: {text}. Press the button again with the same receipt: it only repeats the delivery.")
        else:
            await _save(d, receipt, {"status": "failed", "error": text, "statusCode": code})
            hint = {403: " (the API key and secret must be the tenant's own)", 400: " (check the receipt, address and amount)"}.get(code, "")
            raise BookingError(f"Comet refused the booking{hint}: {text}")
    else:
        data = res.get("data") or {}

    tx, send_tx = data.get("txHash"), data.get("sendTxHash")
    if deliver_to and data.get("recovered") and not send_tx:
        await _save(d, receipt, {"status": "booked", "txHash": tx, "warning": "Comet reports this receipt already booked and did not deliver it."})
        raise BookingError(f"Comet reports this receipt already booked (mint {tx}) and did not send the IMC to your wallet. It sits in Comet's treasury wallet. "
                           "Ask Comet to transfer it to your wallet (a service-token transfer), or contact them with the mint transaction.")
    preview = data.get("preview") or {}
    treasury = data.get("treasury")
    note = None
    full = lambda a: isinstance(a, str) and len(a) == 42
    if not deliver_to and full(treasury) and full(d.treasury_address) and treasury.lower() != d.treasury_address.lower():
        note = (f"Comet minted into {treasury}, which is not your treasury wallet {d.treasury_address}. "
                "The IMC will not appear there. Use the delivery address option to send it to the wallet you want.")
    record = {"status": "booked", "txHash": tx, "sendTxHash": send_tx, "treasury": treasury, "imc": preview.get("imc") or data.get("amount"),
              "airtimeKes": preview.get("airtimeKes"), "usdKes": preview.get("usdKes"), "recovered": bool(data.get("recovered")),
              "error": None, "warning": note}
    await _save(d, receipt, record)
    step(f"Comet booked the mint (transaction {tx})")

    # Confirm on Celo that the IMC really arrived where we expect it, before calling it done.
    arrived: Optional[float] = None
    watch = send_tx if deliver_to else tx
    target_is_treasury = (not deliver_to) or deliver_to.lower() == str(d.treasury_address).lower()
    if watch and target_is_treasury and not note:
        step("Waiting for the IMC to show in the treasury wallet on Celo")
        rec = d.receipts(d.db)
        for _ in range(d.poll_attempts):
            try:
                got = await rec.record_tx(watch)
                if got:
                    arrived = sum(float(g["amountImc"]) for g in got)
                    break
            except Exception:
                pass
            await asyncio.sleep(d.poll_seconds)
    expected = record["imc"]
    warning = note
    if arrived is not None and expected and abs(arrived - float(expected)) > float(expected) * TOLERANCE:
        warning = f"Comet's preview said {float(expected):,.6f} IMC but {arrived:,.6f} IMC arrived on Celo. Check it before relying on the figure."
    elif arrived is None and watch and target_is_treasury and not note:
        warning = "Booked, but the IMC transfer is not visible on Celo yet. It will appear under IMC received when it lands."
    await _save(d, receipt, {"arrivedImc": arrived, "warning": warning})
    return {"receipt": receipt, "txHash": tx, "sendTxHash": send_tx, "imc": record["imc"], "airtimeKes": record["airtimeKes"],
            "usdKes": record["usdKes"], "arrivedImc": arrived, "treasury": treasury, "deliveredTo": deliver_to,
            "recovered": record["recovered"], "warning": warning}


# ---------------------------------------------------------------------------------------------- background job
def _snapshot(job: dict) -> dict:
    return {k: (list(v) if k == "steps" else v) for k, v in job.items()}


async def start(d: Deps, receipt: str, cash_kes: Any, deliver_to: Optional[str], requested_by: str) -> dict:
    """Validates, then books in the background so the browser request returns at once."""
    global _current
    # By default the IMC is sent on to our own wallet: a plain /imc/book leaves it in Comet's treasury wallet, which is
    # not ours (seen 2026-10-07: minted into 0xa4ec…, not the master wallet).
    r, cash, addr = clean_receipt(receipt), clean_cash(cash_kes), (clean_address(deliver_to) or d.treasury_address)
    if _current is not None:
        raise BookingError("A booking is already in progress.")
    existing = await d.db[COLLECTION].find_one({"_id": r})
    if existing and existing.get("status") == "booked" and not _undelivered(existing):
        raise BookingError(f"Receipt {r} was already booked (transaction {existing.get('txHash')}). A receipt books once, ever.")
    job = {"id": uuid.uuid4().hex[:12], "receipt": r, "status": "running", "startedAt": datetime.utcnow().isoformat() + "Z",
           "steps": [], "result": None, "error": None}
    _jobs[job["id"]] = job
    while len(_jobs) > 20:
        _jobs.pop(next(iter(_jobs)))
    _current = job["id"]

    def note(msg: str) -> None:
        job["steps"].append({"at": datetime.utcnow().isoformat() + "Z", "msg": msg})

    async def run() -> None:
        global _current
        try:
            job["result"] = await book(d, r, cash, addr, requested_by, on_step=note)
            job["status"] = "done"
            note("Done")
        except BookingError as e:
            job["status"], job["error"] = "failed", str(e)
        except Exception as e:
            job["status"], job["error"] = "failed", f"Unexpected error: {e}"
        finally:
            job["finishedAt"] = datetime.utcnow().isoformat() + "Z"
            _current = None

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return _snapshot(job)


def latest_job() -> Optional[dict]:
    return _snapshot(_jobs[next(reversed(_jobs))]) if _jobs else None
