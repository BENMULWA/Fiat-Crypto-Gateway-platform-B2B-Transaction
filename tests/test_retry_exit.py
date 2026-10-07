"""Retrying the last step of a pool run whose withdrawal failed. No funds move: the run driver and the chain are doubles."""
import asyncio

import pytest

from workers import corridor_worker as cw


class Runs:
    def __init__(self, doc):
        self.doc = doc

    async def find_one(self, q, proj=None):
        return dict(self.doc) if self.doc and q.get("_id") == self.doc["_id"] else None

    async def update_one(self, q, upd, **kw):
        self.doc.update(upd["$set"])


class DB(dict):
    def __init__(self, doc):
        super().__init__(corridor_runs=Runs(doc))


def stopped_run(**over):
    doc = {"_id": "RUN-X", "status": "HALTED", "currentUsdPrincipal": 0.0301, "haltReason": "Comet rejected the withdrawal",
           "config": {"swap_mode": "pool"}, "exit": None,
           "swap": {"outAmount": 0.030108, "outSymbol": "USDC", "engineWallet": "0xENGINE", "confirmed": True, "txHash": "0xSWAP"}}
    doc.update(over)
    return doc


@pytest.fixture
def driven(monkeypatch):
    started = []

    async def fake_drive(db, run_id):
        started.append(run_id)

    monkeypatch.setattr(cw, "_drive_run", fake_drive)
    return started


def retry(doc, held=0.030108):
    db = DB(doc)

    async def go():
        out = await cw.retry_exit(db, doc["_id"], requested_by="admin-1", balance_fn=lambda sym, addr: held)
        await asyncio.sleep(0)                                    # let the (fake) driver task run
        return out

    return asyncio.run(go())


def test_a_stopped_pool_run_is_resumed_at_its_exit_with_the_swap_amount(driven):
    doc = stopped_run()
    out = retry(doc)
    assert out["status"] == "CELO_EXIT" and out["haltReason"] is None
    assert out["currentUsdPrincipal"] == pytest.approx(0.030108)          # the confirmed amount, not the rounded one
    assert out["exitSwapBooked"] is True and out["exitRetriedBy"] == "admin-1"
    assert driven == ["RUN-X"]


@pytest.mark.parametrize("over,msg", [
    ({"status": "CELO_EXIT"}, "not stopped"),
    ({"config": {"swap_mode": "reserve"}}, "no recorded pool swap"),
    ({"swap": None}, "no recorded pool swap"),
    ({"exit": {"txHash": "0xW", "to": "0xEXIT", "amount": 0.03, "symbol": "USDC"}}, "already went through"),
])
def test_only_a_stopped_pool_run_with_no_withdrawal_can_be_retried(over, msg, driven):
    with pytest.raises(ValueError, match=msg):
        retry(stopped_run(**over))
    assert driven == []


def test_it_refuses_when_the_usdc_is_no_longer_in_the_engine_wallet(driven):
    with pytest.raises(ValueError, match="may already have been withdrawn"):
        retry(stopped_run(), held=0.0)
    assert driven == []


def test_an_unknown_run_is_not_found(driven):
    db = DB(stopped_run())
    with pytest.raises(LookupError):
        asyncio.run(cw.retry_exit(db, "RUN-NOPE", balance_fn=lambda s, a: 1.0))


def test_the_retried_exit_does_not_book_the_swap_a_second_time():
    from Brain_Engine.state_engine import FSMState, HFTCorridorFSM, ImmutableLedger
    import test_pool_swap as tp

    def exit_with(booked):
        bot, ledger, comet, _ = tp.make(receipt=lambda tx, sym: {"amount": 0.079, "pair": "0xP"})
        bot.exit_swap_booked = booked
        bot.current_usd_principal = 0.079
        bot.state = FSMState.CELO_EXIT
        asyncio.run(bot._execute_comet_exit())
        return [e for e in ledger.local_records if e.txn_type.name == "CELO_EXIT"], comet, bot

    first, _, _ = exit_with(False)
    retried, comet, bot = exit_with(True)
    assert len(first) == 1 and len(retried) == 0                           # booked once, not again on the retry
    assert "send USDC" in comet.calls and bot.exit_report["txHash"] == "0xWITHDRAW"
