"""IMC received at the treasury wallet, tracked from the chain's Transfer events.

Why this exists: when Comet will not mint for a tenant, an operator mints IMC from Comet's
dashboard and the tokens arrive in the treasury wallet. A corridor run waiting for that mint used
to detect it by watching the wallet's balance rise from a baseline taken when the wait began. That
cannot see a mint which landed BEFORE the baseline, and it cannot tell WHICH transfer paid for
WHICH run. Here every incoming IMC Transfer is recorded once, with its transaction hash, and a run
claims exactly one of them.

Rules a run must satisfy to claim a receipt:
- the amount is within +/-1% of what the run expects. "At least" would let a large unrelated mint
  be booked against a small run and distort its profit;
- it has not been claimed by another run (a claim is atomic, so a transfer is spent once);
- it arrived after the run started, unless an operator attached the transaction by hash.

Nothing here moves money. It only reads the chain and keeps records.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from typing import Any, Optional

COLLECTION = "imc_receipts"
STATE_COLLECTION = "imc_receipt_state"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # Transfer(address,address,uint256)
IMC_DECIMALS = 6
TOLERANCE = 0.01
# forno.celo.org rejects log queries over about 5,000 blocks.
CHUNK_BLOCKS = int(os.getenv("IMC_SCAN_CHUNK_BLOCKS", "4000"))
LOOKBACK_BLOCKS = int(os.getenv("IMC_SCAN_LOOKBACK_BLOCKS", "60000"))

_TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")


class AttachError(ValueError):
    """The transaction cannot be used for this run; the message says why."""


class HeldError(ValueError):
    """IMC already held cannot be allocated to this run; the message says why."""


# ---------------------------------------------------------------- pure helpers
def hex0x(value: Any) -> str:
    """HexBytes/bytes/str to a 0x-prefixed lowercase string (hexbytes >= 1.0 drops the prefix)."""
    s = value.hex() if hasattr(value, "hex") else str(value)
    s = s.lower()
    return s if s.startswith("0x") else "0x" + s


def pad_topic(address: str) -> str:
    return "0x" + address.lower().replace("0x", "").rjust(64, "0")


def addr_from_topic(topic: Any) -> str:
    return "0x" + hex0x(topic)[-40:]


def parse_transfer(log: Any) -> dict[str, Any]:
    topics = list(log["topics"])
    return {
        "txHash": hex0x(log["transactionHash"]),
        "logIndex": int(log["logIndex"]),
        "blockNumber": int(log["blockNumber"]),
        "from": addr_from_topic(topics[1]),
        "to": addr_from_topic(topics[2]),
        "amountBase": int(hex0x(log["data"]), 16),
        "amountImc": int(hex0x(log["data"]), 16) / (10 ** IMC_DECIMALS),
    }


def within(amount: float, expected: float, tol: float = TOLERANCE) -> bool:
    return expected > 0 and abs(amount - expected) <= expected * tol


def normalize_tx_hash(raw: str) -> str:
    h = (raw or "").strip()
    if not h.startswith("0x") and re.fullmatch(r"[0-9a-fA-F]{64}", h):
        h = "0x" + h
    if not _TX_HASH.match(h):
        raise AttachError("That is not a transaction hash. It should be 0x followed by 64 hex characters.")
    return h.lower()


def _modified(result: Any) -> int:
    if isinstance(result, dict):
        return int(result.get("modified_count", 0))
    return int(getattr(result, "modified_count", 0))


# ---------------------------------------------------------------- service
class ImcReceipts:
    """Records incoming IMC transfers and lets runs claim them. `w3`, `token` and `treasury` are
    resolved lazily from the app's Celo setup unless injected (tests inject fakes)."""

    def __init__(self, db, w3: Any = None, token: Optional[str] = None, treasury: Optional[str] = None,
                 balance_fn: Any = None, booked_fn: Any = None):
        self.db = db
        self._w3, self._token, self._treasury = w3, token, treasury
        # Injectable for tests: the treasury's real IMC balance, and the IMC the ledger already books as
        # vault-backed. The defaults read the chain and the ledger.
        self._balance_fn, self._booked_fn = balance_fn, booked_fn

    # -- lazy wiring
    def _wire(self) -> None:
        if self._w3 is None or self._token is None or self._treasury is None:
            from routes.valora import ASSET_CONTRACTS, get_treasury_address, w3 as celo_w3
            self._w3 = self._w3 or celo_w3
            self._token = self._token or ASSET_CONTRACTS["IMC"]
            self._treasury = self._treasury or get_treasury_address()

    @property
    def treasury(self) -> str:
        self._wire()
        return self._treasury  # type: ignore[return-value]

    # -- chain reads (blocking web3 calls run in a thread)
    async def _block_time(self, block_number: int) -> datetime:
        blk = await asyncio.to_thread(self._w3.eth.get_block, block_number)
        return datetime.utcfromtimestamp(int(blk["timestamp"]))

    async def _record(self, t: dict[str, Any], source: str) -> tuple[dict[str, Any], bool]:
        """Insert a transfer once. Returns (stored record, created?)."""
        rid = f"{t['txHash']}:{t['logIndex']}"
        existing = await self.db[COLLECTION].find_one({"_id": rid})
        if existing:
            return existing, False
        doc = {
            "_id": rid, **t,
            "blockTime": await self._block_time(t["blockNumber"]),
            "observedAt": datetime.utcnow(),
            "source": source,
            "claimedByRun": None, "claimedAt": None, "consumed": False,
        }
        try:
            await self.db[COLLECTION].insert_one(doc)
        except Exception:
            # another scanner recorded it first (duplicate _id): use theirs
            existing = await self.db[COLLECTION].find_one({"_id": rid})
            if existing:
                return existing, False
            raise
        return doc, True

    async def scan(self, lookback_blocks: Optional[int] = None) -> int:
        """Record every IMC Transfer to the treasury since the last scan. Returns how many are new."""
        self._wire()
        latest = int(await asyncio.to_thread(lambda: self._w3.eth.block_number))
        state = await self.db[STATE_COLLECTION].find_one({"_id": "cursor"})
        start = (state["lastBlock"] + 1) if state else max(0, latest - (lookback_blocks or LOOKBACK_BLOCKS))
        created = 0
        cur = start
        while cur <= latest:
            end = min(cur + CHUNK_BLOCKS - 1, latest)
            logs = await asyncio.to_thread(
                self._w3.eth.get_logs,
                {"fromBlock": cur, "toBlock": end, "address": self._token,
                 "topics": [TRANSFER_TOPIC, None, pad_topic(self._treasury)]},
            )
            for log in logs:
                _, new = await self._record(parse_transfer(log), "scan")
                created += int(new)
            cur = end + 1
        if state:
            await self.db[STATE_COLLECTION].update_one({"_id": "cursor"}, {"$set": {"lastBlock": latest, "updatedAt": datetime.utcnow()}})
        else:
            await self.db[STATE_COLLECTION].insert_one({"_id": "cursor", "lastBlock": latest, "updatedAt": datetime.utcnow()})
        return created

    async def transfers_in_tx(self, tx_hash: str) -> list[dict[str, Any]]:
        """IMC transfers to the treasury inside one transaction, read from its receipt. Raises
        AttachError if the transaction is unknown, still pending, or failed."""
        self._wire()
        h = normalize_tx_hash(tx_hash)
        try:
            receipt = await asyncio.to_thread(self._w3.eth.get_transaction_receipt, h)
        except Exception:
            raise AttachError("That transaction was not found on Celo yet. Check the hash, or wait a few seconds.")
        if int(receipt["status"]) != 1:
            raise AttachError("That transaction failed on-chain, so no IMC moved.")
        out = []
        for log in receipt["logs"]:
            topics = list(log["topics"])
            if (len(topics) == 3 and hex0x(topics[0]) == TRANSFER_TOPIC
                    and str(log["address"]).lower() == str(self._token).lower()
                    and addr_from_topic(topics[2]).lower() == str(self._treasury).lower()):
                out.append(parse_transfer(log))
        return out

    # -- claims
    async def _claim(self, rid: str, run_id: str, now: datetime) -> bool:
        res = await self.db[COLLECTION].update_one(
            {"_id": rid, "claimedByRun": None},
            {"$set": {"claimedByRun": run_id, "claimedAt": now, "consumed": False}},
        )
        return _modified(res) == 1

    async def reserved_for(self, run_id: str) -> Optional[dict[str, Any]]:
        """A receipt already attached to this run and not yet booked."""
        return await self.db[COLLECTION].find_one({"claimedByRun": run_id, "consumed": False})

    async def claim_match(self, run_id: str, expected_imc: float, since: Optional[datetime]) -> Optional[dict[str, Any]]:
        """Claim the oldest unclaimed receipt within +/-1% of `expected_imc` that arrived after `since`."""
        cands = await self.db[COLLECTION].find({"claimedByRun": None}).sort("blockTime", 1).to_list(length=200)
        for r in cands:
            if not within(r["amountImc"], expected_imc):
                continue
            if since is not None and r["blockTime"] < since:
                continue
            if await self._claim(r["_id"], run_id, datetime.utcnow()):
                return {**r, "claimedByRun": run_id}
        return None

    async def find_for_run(self, run_id: str, expected_imc: float, since: Optional[datetime]) -> Optional[dict[str, Any]]:
        return await self.reserved_for(run_id) or await self.claim_match(run_id, expected_imc, since)

    async def mark_consumed(self, rid: str) -> None:
        await self.db[COLLECTION].update_one({"_id": rid}, {"$set": {"consumed": True, "consumedAt": datetime.utcnow()}})

    async def attach(self, run_id: str, tx_hash: str, expected_imc: float) -> dict[str, Any]:
        """Operator supplies the transaction hash. Verified on-chain, then reserved for the run. This is
        how a mint that landed before the run began is used, so the 'arrived after the run started' rule
        is not applied; the amount rule still is."""
        transfers = await self.transfers_in_tx(tx_hash)
        if not transfers:
            raise AttachError("That transaction did not send IMC to the treasury wallet.")
        recorded = [(await self._record(t, "attach"))[0] for t in transfers]
        match = next((r for r in recorded if within(r["amountImc"], expected_imc)), None)
        if match is None:
            got = ", ".join(f"{r['amountImc']:,.6f}" for r in recorded)
            raise AttachError(
                f"That transaction sent {got} IMC, but this run expects {expected_imc:,.6f} IMC (within 1%). "
                "A different amount cannot be booked against this run."
            )
        if match.get("claimedByRun") and match["claimedByRun"] != run_id:
            raise AttachError(f"That IMC was already used by run {match['claimedByRun']}.")
        if match.get("claimedByRun") == run_id:
            return match
        if not await self._claim(match["_id"], run_id, datetime.utcnow()):
            raise AttachError("That IMC was just claimed by another run.")
        return {**match, "claimedByRun": run_id}

    async def mark_booked(self, receipt_id: str, run_id: str, ledger_txn_id: str, note: str) -> dict[str, Any]:
        """Link a receipt to a ledger MINT that already used it (for example one booked by the earlier
        balance-based wait, which did not record the transaction hash). This books nothing: the ledger
        already has the entry. It only stops the same IMC being claimed a second time.
        Refuses if the receipt is unknown or already used."""
        rec = await self.db[COLLECTION].find_one({"_id": receipt_id})
        if not rec:
            raise LookupError(f"Unknown receipt: {receipt_id}")
        if rec.get("claimedByRun"):
            raise ValueError(f"Receipt {receipt_id} is already {'used by' if rec.get('consumed') else 'reserved for'} {rec['claimedByRun']}.")
        if not await self._claim(receipt_id, run_id, datetime.utcnow()):
            raise ValueError(f"Receipt {receipt_id} was just claimed by another run.")
        update = {"consumed": True, "consumedAt": datetime.utcnow(), "ledgerTxnId": ledger_txn_id, "note": note}
        await self.db[COLLECTION].update_one({"_id": receipt_id}, {"$set": update})
        return {**rec, "claimedByRun": run_id, **update}

    # -- IMC already held (no new mint)
    async def free_imc(self) -> dict[str, float]:
        """IMC in the treasury that no run has used: the real on-chain balance, minus what the ledger already
        books as vault-backed, minus what waiting runs have reserved but not yet booked."""
        if self._balance_fn is not None:
            balance = float(await asyncio.to_thread(self._balance_fn))
        else:
            from Brain_Engine.state_engine import _get_real_treasury_imc_balance
            balance = float(await asyncio.to_thread(_get_real_treasury_imc_balance))
        if self._booked_fn is not None:
            booked = float(await self._booked_fn())
        else:
            from Brain_Engine.state_engine import ImmutableLedger
            booked = float(await ImmutableLedger(db_collection=self.db["transactions"]).get_vault_backed_total("N7", "IMC"))
        pending = await self.db[COLLECTION].find({"consumed": False}).to_list(length=500)
        reserved = sum(float(r["amountImc"]) for r in pending if r.get("claimedByRun"))
        return {"balance": balance, "booked": booked, "reserved": reserved, "free": balance - booked - reserved}

    async def allocate_held(self, run_id: str, expected_imc: float) -> dict[str, Any]:
        """Reserve `expected_imc` of the IMC the treasury already holds for a run that is waiting for its
        mint, so the run can continue without a new mint. It books exactly what the run expects, never more.
        Refuses unless that much IMC is free. Idempotent for the same run."""
        existing = await self.db[COLLECTION].find_one({"_id": f"HELD-{run_id}"})
        if existing:
            return existing
        if not expected_imc or expected_imc <= 0:
            raise HeldError("This run has no expected mint amount recorded.")
        stock = await self.free_imc()
        if stock["free"] + 1e-9 < expected_imc:
            raise HeldError(
                f"Only {max(stock['free'], 0):,.6f} IMC is free (wallet {stock['balance']:,.6f}, already booked "
                f"{stock['booked']:,.6f}, reserved {stock['reserved']:,.6f}), but this run needs {expected_imc:,.6f}."
            )
        now = datetime.utcnow()
        doc = {
            "_id": f"HELD-{run_id}", "txHash": f"HELD-{run_id}", "logIndex": 0, "blockNumber": None,
            "from": "treasury (already held)", "to": self.treasury, "amountImc": float(expected_imc),
            "amountBase": int(round(expected_imc * 10 ** IMC_DECIMALS)),
            "blockTime": now, "observedAt": now, "source": "held",
            "claimedByRun": run_id, "claimedAt": now, "consumed": False,
        }
        await self.db[COLLECTION].insert_one(doc)
        return doc

    async def release_run(self, run_id: str) -> int:
        """Free what a run had reserved but not booked (for example when it is cancelled). IMC allocated from
        holdings is deleted; a reserved on-chain transfer becomes claimable again."""
        n = 0
        for r in await self.db[COLLECTION].find({"claimedByRun": run_id, "consumed": False}).to_list(length=50):
            if r.get("source") == "held":
                await self.db[COLLECTION].delete_one({"_id": r["_id"]})
            else:
                await self.db[COLLECTION].update_one({"_id": r["_id"]}, {"$set": {"claimedByRun": None, "claimedAt": None}})
            n += 1
        return n

    async def record_tx(self, tx_hash: str) -> list[dict[str, Any]]:
        """Record a transaction's incoming IMC without claiming it for any run."""
        return [(await self._record(t, "manual"))[0] for t in await self.transfers_in_tx(tx_hash)]

    async def list_receipts(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self.db[COLLECTION].find({}).sort("blockTime", -1).limit(limit).to_list(length=limit)
