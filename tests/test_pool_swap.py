"""The corridor's pool mode: sell the minted IMC for USDC in Comet's AMM pool, then only withdraw at the exit.

Comet, the chain and the transfers are in-memory doubles: no swap, no transfer, no network."""
import asyncio

import pytest

from Brain_Engine.state_engine import FSMState, HFTCorridorFSM, ImmutableLedger

ENGINE_WALLET = "0xa0885b9c378c393dbf7f1d8e85510eca6e611e9a"


def run(coro):
    return asyncio.run(coro)


class FakeComet:
    """Stands in for Comet: a 1:1 pool whose output is imc * (1 - loss)."""

    def __init__(self, loss=0.015, quote_status="success", swap_status="success", wallet_ok=True):
        self.loss, self.quote_status, self.swap_status, self.wallet_ok = loss, quote_status, swap_status, wallet_ok
        self.calls = []

    @staticmethod
    def to_base_units(amount, decimals=6):
        return str(int(round(amount * 10 ** decimals)))

    @staticmethod
    def from_base_units(amount_base, decimals=6):
        return float(amount_base) / 10 ** decimals

    def get_or_create_wallet(self, uid, chain="celo"):
        self.calls.append("wallet")
        return {"status": "success", "data": {"address": ENGINE_WALLET}} if self.wallet_ok else {"status": "error", "message": "no wallet"}

    def get_amm_quote(self, a, b, amount_in):
        self.calls.append(f"quote {a}->{b}")
        if self.quote_status != "success":
            return {"status": "error", "message": "getAmountsOut: INSUFFICIENT_LIQUIDITY"}
        out = int(int(amount_in) * (1 - self.loss))
        return {"status": "success", "data": {"amountOut": str(out)}}

    def execute_amm_swap(self, external_user_id, from_symbol, to_symbol, amount_in, **kw):
        self.calls.append(f"swap {from_symbol}->{to_symbol}")
        if self.swap_status != "success":
            return {"status": "error", "message": "wallet holds 0.000000 IMC"}
        return {"status": "success", "data": {"txHash": "0xSWAP"}}

    def send_asset(self, symbol, external_user_id, to, amount_base):
        self.calls.append(f"send {symbol}")
        return {"status": "success", "data": {"txHash": "0xWITHDRAW"}}


class Wallet:
    """The engine's Comet wallet balance and the treasury transfers that fill it."""

    def __init__(self, held=0.0, fund_fails=False, arrives=True):
        self.held, self.fund_fails, self.arrives, self.sent = held, fund_fails, arrives, []

    def balance(self, address):
        assert address == ENGINE_WALLET
        return self.held

    async def fund(self, address, amount):
        if self.fund_fails:
            raise RuntimeError("treasury transfer failed")
        self.sent.append(amount)
        if self.arrives:
            self.held += amount
        return "0xFUND"


def make(comet=None, wallet=None, swap_mode="pool", slippage=0.02, principal=0.0772, receipt=None):
    comet = comet or FakeComet()
    wallet = wallet or Wallet()
    ledger = ImmutableLedger(db_collection=None)
    bot = HFTCorridorFSM(ledger, starting_capital_usd=principal, config={
        "node_procure": "N2", "node_liquidate": "N5", "discount": 0.05, "baseline_rate": 129.50, "run_id": "RUN-P",
        "comet_client": comet, "swap_mode": swap_mode, "pool_max_slippage": slippage,
        "comet_wallet_imc_balance_fn": wallet.balance, "fund_comet_wallet_fn": wallet.fund,
        "pool_fund_poll_attempts": 2, "pool_fund_poll_seconds": 0.001, "comet_user_id": 254,
        "comet_exit_address": "0xEXIT",
        "swap_receipt_fn": receipt or (lambda tx, sym: None),   # no chain access: the quote is used
    })
    return bot, ledger, comet, wallet


def swap(bot, imc=0.081081):
    run(bot._finish_mint_with_internal_swap(imc, 0.0))


def test_a_good_pool_sells_the_imc_and_moves_the_run_on():
    bot, ledger, comet, wallet = make()
    swap(bot)
    assert bot.state == FSMState.ROLLOVER
    assert bot.current_usd_principal == pytest.approx(0.081081 * 0.985, abs=2e-6)   # the USDC the pool pays (whole base units)
    assert comet.calls == ["wallet", "quote IMC->USDC", "swap IMC->USDC"]          # quote comes before the swap
    assert wallet.sent == [pytest.approx(0.081081)]                                # the engine wallet was topped up
    # realised result, after the pool's fee and price impact (not the optimistic mint profit)
    assert bot.last_cycle_pnl_usd == pytest.approx(0.081081 * 0.985 - 0.0772, abs=2e-6)
    assets = [(e.from_node, e.to_node, e.asset) for e in ledger.local_records]
    assert ("N7", "AMM", "IMC") in assets and ("AMM", "N7", "USDC") in assets


def test_a_thin_pool_halts_before_any_imc_moves():
    bot, ledger, comet, wallet = make(comet=FakeComet(loss=0.05))                  # 5% below $1: over the 2% guard
    swap(bot)
    assert bot.state == FSMState.HALTED and "RISK LIMIT" in bot.halt_reason and "stays in the treasury" in bot.halt_reason
    assert wallet.sent == []                                                       # nothing was transferred
    assert "swap IMC->USDC" not in comet.calls                                     # and nothing was swapped


def test_an_empty_pool_halts_cleanly_with_the_reason():
    bot, ledger, comet, wallet = make(comet=FakeComet(quote_status="error"))
    swap(bot)
    assert bot.state == FSMState.HALTED and "INSUFFICIENT_LIQUIDITY" in bot.halt_reason
    assert wallet.sent == [] and comet.calls == ["wallet", "quote IMC->USDC"]


def test_the_guard_can_be_widened_for_a_test_run():
    bot, *_ = make(comet=FakeComet(loss=0.03), slippage=0.05)
    swap(bot)
    assert bot.state == FSMState.ROLLOVER


def test_an_already_funded_engine_wallet_is_not_topped_up_again():
    bot, ledger, comet, wallet = make(wallet=Wallet(held=0.5))
    swap(bot)
    assert bot.state == FSMState.ROLLOVER and wallet.sent == []


def test_only_the_shortfall_is_sent():
    bot, ledger, comet, wallet = make(wallet=Wallet(held=0.05))
    swap(bot, imc=0.081081)
    assert wallet.sent == [pytest.approx(0.031081)]


def test_a_failed_treasury_transfer_halts_before_the_swap():
    bot, ledger, comet, wallet = make(wallet=Wallet(fund_fails=True))
    swap(bot)
    assert bot.state == FSMState.HALTED and "stays in the treasury" in bot.halt_reason
    assert "swap IMC->USDC" not in comet.calls


def test_imc_that_never_shows_up_in_the_engine_wallet_halts_without_swapping():
    bot, ledger, comet, wallet = make(wallet=Wallet(arrives=False))
    swap(bot)
    assert bot.state == FSMState.HALTED and "not visible there yet" in bot.halt_reason and "not lost" in bot.halt_reason
    assert "swap IMC->USDC" not in comet.calls


def test_a_rejected_swap_halts_and_says_the_imc_is_safe():
    bot, ledger, comet, wallet = make(comet=FakeComet(swap_status="error"))
    swap(bot)
    assert bot.state == FSMState.HALTED and "not lost" in bot.halt_reason and "holds 0.000000 IMC" in bot.halt_reason


def test_a_missing_comet_wallet_halts():
    bot, *_ = make(comet=FakeComet(wallet_ok=False))
    swap(bot)
    assert bot.state == FSMState.HALTED and "engine's Comet wallet" in bot.halt_reason


def test_reserve_mode_is_untouched_and_never_calls_the_pool():
    async def rate(a, b):
        return 1.0

    comet = FakeComet()
    bot = HFTCorridorFSM(ImmutableLedger(db_collection=None), starting_capital_usd=0.0772, config={
        "node_procure": "N2", "node_liquidate": "N5", "discount": 0.05, "baseline_rate": 129.50, "run_id": "RUN-R",
        "comet_client": comet, "get_swap_rate_fn": rate, "get_treasury_usdt_balance_fn": lambda: 100.0, "comet_user_id": 254,
    })
    assert bot.SWAP_MODE == "reserve"
    swap(bot)
    assert bot.state == FSMState.ROLLOVER and comet.calls == []


def test_the_pool_exit_only_withdraws_and_does_not_swap_again():
    bot, ledger, comet, wallet = make()
    swap(bot)                                                                      # IMC sold, USDC in Comet custody
    comet.calls.clear()
    bot.state = FSMState.CELO_EXIT
    run(bot._execute_comet_exit())
    assert bot.state == FSMState.COMPLETED
    assert comet.calls == ["send USDC"]                                            # no quote, no USDT->USDC swap
    exits = [e for e in ledger.local_records if e.txn_type.value == "CELO_EXIT"]
    assert len(exits) == 1 and exits[0].amount == pytest.approx(0.081081 * 0.985, abs=2e-6)


def test_the_reserve_exit_still_swaps_usdt_to_usdc_as_before():
    comet = FakeComet()
    bot = HFTCorridorFSM(ImmutableLedger(db_collection=None), starting_capital_usd=0.4, config={
        "node_procure": "N2", "node_liquidate": "N5", "discount": 0.05, "baseline_rate": 129.50, "run_id": "RUN-X",
        "comet_client": comet, "comet_user_id": 254, "comet_exit_address": "0xEXIT",
    })
    bot.state = FSMState.CELO_EXIT
    run(bot._execute_comet_exit())
    assert comet.calls == ["quote USDT->USDC", "swap USDT->USDC", "send USDC"]
    assert bot.state == FSMState.COMPLETED


# ------------------------------------------------------------------ the read-only pool check endpoint
def _pool_check(monkeypatch, role):
    import routes.market_maker as mm
    from services import imc_receipts
    from Brain_Engine import state_engine
    from routes.valora import ASSET_CONTRACTS

    class C(FakeComet):
        def list_amm_pools(self):
            return {"status": "success", "data": [{"token0": ASSET_CONTRACTS["IMC"], "token1": ASSET_CONTRACTS["USDC"], "reserve0": "2000000", "reserve1": "2000000"}]}

    fake = C(loss=0.02)
    monkeypatch.setattr(mm, "comet", fake)
    monkeypatch.setattr(state_engine, "_real_celo_imc_balance_of", lambda addr: 0.0)

    async def free(self):
        return {"free": 2.0, "balance": 2.5, "booked": 0.5, "reserved": 0.0}

    monkeypatch.setattr(imc_receipts.ImcReceipts, "free_imc", free)
    out = run(mm.pool_check(imc=0.0405, to_symbol="USDC", max_slippage=0.03, db=object(), current_user={"role": role}))
    return out, fake


def test_pool_check_shows_the_quote_to_everyone_but_hides_operator_details_from_investors(monkeypatch):
    out, fake = _pool_check(monkeypatch, "institutional")
    assert out["pool"]["seeded"] and out["quote"]["ok"] and out["quote"]["withinGuard"]      # 2% < the 3% limit
    assert out["engineWallet"]["address"] is None and out["masterFreeImc"] is None
    assert out["needsFunding"] is False and out["canFund"] is True
    assert "wallet" not in fake.calls                                                           # the engine wallet was never looked up


def test_pool_check_gives_operators_the_wallet_and_funding_picture(monkeypatch):
    out, fake = _pool_check(monkeypatch, "super_admin")
    assert out["engineWallet"]["address"] == ENGINE_WALLET and out["masterFreeImc"] == 2.0
    assert out["needsFunding"] is True and out["canFund"] is True and out["shortfall"] == pytest.approx(0.0405)


# ------------------------------------------------------------------ the confirmed amount from Celo
def test_the_real_amount_read_from_celo_replaces_the_quote():
    seen = []

    def receipt(tx, sym):
        seen.append((tx, sym))
        return {"amount": 0.079000, "pair": "0xPAIR"}                              # the pool paid a little less than quoted

    bot, ledger, comet, wallet = make(receipt=receipt)
    swap(bot)
    assert seen == [("0xSWAP", "USDC")]
    assert bot.current_usd_principal == pytest.approx(0.079000)                    # the run carries what was really paid
    assert bot.last_cycle_pnl_usd == pytest.approx(0.079000 - 0.0772)
    r = bot.swap_report
    assert r["confirmed"] is True and r["outAmount"] == pytest.approx(0.079) and r["quotedOut"] == pytest.approx(0.081081 * 0.985, abs=2e-6)
    assert r["txHash"] == "0xSWAP" and r["pair"] == "0xPAIR" and r["engineWallet"] == ENGINE_WALLET
    usdc_in = [e for e in ledger.local_records if e.asset == "USDC"][0]
    assert usdc_in.amount == pytest.approx(0.079)                                   # the ledger books the real figure


def test_when_celo_cannot_be_read_the_quote_is_used_and_marked_as_an_estimate():
    bot, *_ = make()                                                               # default fake: unreadable
    swap(bot)
    assert bot.state == FSMState.ROLLOVER
    assert bot.swap_report["confirmed"] is False and bot.swap_report["outAmount"] == pytest.approx(0.081081 * 0.985, abs=2e-6)

    def boom(tx, sym):
        raise RuntimeError("rpc down")

    bot2, *_ = make(receipt=boom)
    swap(bot2)                                                                     # a reader that crashes does not stop the run
    assert bot2.state == FSMState.ROLLOVER and bot2.swap_report["confirmed"] is False


def test_the_exit_records_the_withdrawal_hash_and_destination():
    bot, ledger, comet, wallet = make(receipt=lambda tx, sym: {"amount": 0.079, "pair": "0xPAIR"})
    swap(bot)
    bot.state = FSMState.CELO_EXIT
    run(bot._execute_comet_exit())
    assert bot.exit_report == {"txHash": "0xWITHDRAW", "to": "0xEXIT", "amount": pytest.approx(0.079), "symbol": "USDC"}
    assert "send USDC" in comet.calls


def test_the_swap_receipt_reader_adds_up_the_pairs_usdc_payout(monkeypatch):
    """Parses a real-shaped receipt: a pair's Swap event, the pair's USDC transfer out, and an unrelated transfer."""
    from types import SimpleNamespace
    from hexbytes import HexBytes
    from web3 import Web3
    from Brain_Engine import state_engine
    import routes.valora as valora

    pair, other = "0x1A182095075Aad59F8013879b4F4341665fE7A77", "0x9999999999999999999999999999999999999999"
    usdc = valora.ASSET_CONTRACTS["USDC"]
    t_swap = Web3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)")
    t_xfer = Web3.keccak(text="Transfer(address,address,uint256)")
    pad = lambda a: HexBytes("0x" + a.lower().removeprefix("0x").rjust(64, "0"))
    amt = lambda v: HexBytes(hex(v)[2:].rjust(64, "0"))
    logs = [
        {"address": pair, "topics": [t_swap, pad(other), pad(other)], "data": HexBytes(b"\x00" * 128)},
        {"address": usdc, "topics": [t_xfer, pad(pair), pad(other)], "data": amt(263432)},   # the pair pays out 0.263432
        {"address": usdc, "topics": [t_xfer, pad(other), pad(pair)], "data": amt(999999)},   # a transfer INTO the pair: ignored
    ]
    fake = SimpleNamespace(eth=SimpleNamespace(get_transaction_receipt=lambda tx: {"status": 1, "logs": logs}))
    monkeypatch.setattr(valora, "w3", fake)
    out = state_engine._real_swap_receipt("0xabc", "USDC", attempts=1, delay=0)
    assert out == {"amount": pytest.approx(0.263432), "pair": pair.lower()}

    reverted = SimpleNamespace(eth=SimpleNamespace(get_transaction_receipt=lambda tx: {"status": 0, "logs": logs}))
    monkeypatch.setattr(valora, "w3", reverted)
    assert state_engine._real_swap_receipt("0xabc", "USDC", attempts=1, delay=0) is None


# ------------------------------------------------------------------ the reports are saved and restored with the run
def test_the_swap_and_exit_reports_are_saved_with_the_run_and_restored_on_resume():
    from workers import corridor_worker as cw
    bot, *_ = make(receipt=lambda tx, sym: {"amount": 0.079, "pair": "0xPAIR"})
    swap(bot)
    bot.state = FSMState.CELO_EXIT
    run(bot._execute_comet_exit())

    saved = {}

    class Runs:
        async def update_one(self, flt, upd, **kw):
            saved.update(upd["$set"])

        async def find_one(self, flt, projection=None):
            return {"startingUsd": 0.0772}

    run(cw._persist_tick({"corridor_runs": Runs()}, "RUN-P", bot))
    assert saved["swap"]["txHash"] == "0xSWAP" and saved["swap"]["confirmed"] is True
    assert saved["exit"]["txHash"] == "0xWITHDRAW" and saved["exit"]["to"] == "0xEXIT"

    # a run that restarts mid-way gets them back, so a later tick does not wipe them
    cfg = {"comet_client": FakeComet(), "resume_swap_report": saved["swap"], "resume_exit_report": saved["exit"], "run_id": "RUN-P"}
    again = HFTCorridorFSM(ImmutableLedger(db_collection=None), starting_capital_usd=0.0772, config=cfg)
    assert again.swap_report == saved["swap"] and again.exit_report == saved["exit"]
