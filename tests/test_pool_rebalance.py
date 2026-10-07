"""The pool rebalance: planning maths, refusals, and the swap sequence across both IMC pools. Comet, Celo and
the treasury are in-memory doubles, so nothing here moves funds or touches a network."""
import asyncio

import pytest

from services import pool_rebalance as pr

CONTRACTS = {"IMC": "0xIMC", "USDC": "0xUSDC", "USDT": "0xUSDT"}
ENGINE = "0xENGINE"
TREASURY = "0xTREASURY"


def run(coro):
    return asyncio.run(coro)


def listing(imc=3.794897, usdc=1.055709, usdt_pool=(0.749538, 2.705103)):
    """Comet's pool listing: IMC (token0) / USDC (token1), and USDT (token0) / IMC (token1) as on the live chain."""
    pools = [{"token0": "0xIMC", "token1": "0xUSDC", "pair": "0xPAIR", "reserve0": str(int(imc * 1e6)), "reserve1": str(int(usdc * 1e6))}]
    if usdt_pool:
        pools.append({"token0": "0xUSDT", "token1": "0xIMC", "pair": "0xPAIR2", "reserve0": str(int(usdt_pool[0] * 1e6)), "reserve1": str(int(usdt_pool[1] * 1e6))})
    return {"status": "success", "data": {"count": len(pools), "pools": pools}}


class FakeComet:
    def __init__(self, pools, quote_scale=1.0, fail_pair=None):
        self.pools, self.quote_scale, self.fail_pair, self.calls = pools, quote_scale, fail_pair, []

    to_base_units = staticmethod(lambda a: str(int(round(a * 1e6))))
    from_base_units = staticmethod(lambda a: float(a) / 1e6)

    def list_amm_pools(self):
        return self.pools

    def get_or_create_wallet(self, uid):
        return {"status": "success", "data": {"address": ENGINE}}

    def _pool_for(self, a, b):
        return next(p for p in pr.read_pools(self.pools, CONTRACTS) if {p["a"], p["b"]} == {a, b})

    def get_amm_quote(self, a, b, amount_in):
        self.calls.append(f"quote {a}->{b}")
        pool = self._pool_for(a, b)
        out = pr._out(int(amount_in) / 1e6, pr.reserves(pool, a), pr.reserves(pool, b))
        return {"status": "success", "data": {"amountOut": str(int(out * self.quote_scale * 1e6))}}

    def execute_amm_swap(self, external_user_id, from_symbol, to_symbol, amount_in, **kw):
        self.calls.append(f"swap {from_symbol}->{to_symbol}")
        if self.fail_pair == (from_symbol, to_symbol):
            return {"status": "error", "message": "wallet holds 0.000000"}
        return {"status": "success", "data": {"txHash": f"0xSWAP-{from_symbol}-{to_symbol}"}}


class Chain:
    def __init__(self, engine=None, treasury=None, arrives=True, receipts=None):
        self.bal = {("USDC", TREASURY): 3.0, ("USDT", TREASURY): 1.0, ("IMC", TREASURY): 2.0}
        self.bal.update({(s, ENGINE): v for s, v in (engine or {}).items()})
        self.bal.update({(s, TREASURY): v for s, v in (treasury or {}).items()})
        self.arrives, self.sent, self.receipts = arrives, [], receipts or {}

    def balance(self, symbol, address):
        return self.bal.get((symbol, address), 0.0)

    async def fund(self, symbol, address, amount):
        self.sent.append((symbol, address, amount))
        sent = int(amount * 10 ** 6) / 10 ** 6                      # exactly what settle_crypto_on_celo does: truncate to whole units
        if self.arrives:
            self.bal[(symbol, address)] = self.bal.get((symbol, address), 0.0) + sent
        return f"0xFUND-{symbol}"

    def receipt_fn(self, tx, symbol):
        return self.receipts.get(tx)


class Runs:
    def __init__(self, live=None):
        self.live = live

    async def find_one(self, q, proj=None):
        return {"_id": self.live} if self.live else None


class Audit:
    def __init__(self):
        self.rows = []

    async def insert_one(self, doc):
        self.rows.append(doc)


class DB(dict):
    def __init__(self, live=None):
        super().__init__(corridor_runs=Runs(live), pool_rebalances=Audit())


def deps(comet, chain, db=None):
    return pr.Deps(comet=comet, contracts=CONTRACTS, balance_fn=chain.balance, fund_fn=chain.fund, receipt_fn=chain.receipt_fn,
                   user_id="254", treasury_address=TREASURY, db=db if db is not None else DB(), poll_attempts=2, poll_seconds=0.001)


def acts(p):
    return {a["pair"]: a for a in p["actions"]}


# --------------------------------------------------------------------------------------------- the plan
def test_your_live_pools_each_get_one_swap_that_brings_them_to_one_dollar():
    p = pr.plan(pr.read_pools(listing(), CONTRACTS))
    a = acts(p)
    assert list(a) == ["IMC/USDT", "IMC/USDC"]                                    # the other pool first, the main one last
    assert a["IMC/USDC"]["spend"] == "USDC" and a["IMC/USDC"]["spendAmount"] == pytest.approx(0.9487, abs=2e-3)
    assert a["IMC/USDT"]["spend"] == "USDT" and a["IMC/USDT"]["spendAmount"] == pytest.approx(0.6764, abs=2e-3)
    assert a["IMC/USDT"]["receiveAmount"] == pytest.approx(1.28, abs=0.02)
    for x in a.values():
        assert x["priceAfter"] == pytest.approx(1.0, abs=0.01)


def test_a_pool_that_is_too_dear_is_fixed_by_selling_imc():
    p = pr.plan(pr.read_pools(listing(imc=1.0, usdc=4.0, usdt_pool=None), CONTRACTS))
    a = acts(p)["IMC/USDC"]
    assert (a["spend"], a["receive"]) == ("IMC", "USDC") and a["priceAfter"] == pytest.approx(1.0, abs=0.01)


def test_balanced_pools_need_nothing():
    p = pr.plan(pr.read_pools(listing(imc=2.0, usdc=2.0, usdt_pool=(2.0, 2.0)), CONTRACTS))
    assert p["balanced"] and p["actions"] == []


def test_an_empty_main_pool_is_a_blocker():
    p = pr.plan(pr.read_pools(listing(imc=0, usdc=0, usdt_pool=None), CONTRACTS))
    assert not p["seeded"] and "empty" in p["blockers"][0]


def test_dust_in_the_other_pool_is_left_alone():
    p = pr.plan(pr.read_pools(listing(usdt_pool=(0.0005, 0.0019)), CONTRACTS))
    assert list(acts(p)) == ["IMC/USDC"]


def test_a_swap_larger_than_the_cap_is_a_blocker():
    p = pr.plan(pr.read_pools(listing(imc=400.0, usdc=10.0, usdt_pool=None), CONTRACTS), max_spend=5.0)
    assert any("cap" in b for b in p["blockers"])


# --------------------------------------------------------------------------------------------- the preview
def test_the_preview_moves_nothing_and_lists_what_each_token_needs():
    comet, chain = FakeComet(listing()), Chain()
    out = run(pr.preview(deps(comet, chain)))
    assert out["canExecute"] is True and out["reasons"] == []
    need = {n["symbol"]: n for n in out["needs"]}
    assert need["USDC"]["amount"] == pytest.approx(0.9487, abs=2e-3) and need["USDT"]["amount"] == pytest.approx(0.6764, abs=2e-3)
    assert chain.sent == [] and not any(c.startswith("swap") for c in comet.calls)       # looking never moves anything


def test_the_preview_says_how_much_to_send_when_the_treasury_is_short():
    out = run(pr.preview(deps(FakeComet(listing()), Chain(treasury={"USDT": 0.431126}))))     # your real treasury USDT
    assert not out["canExecute"]
    msg = next(r for r in out["reasons"] if "USDT" in r)
    assert "Send about 0.25 more USDT" in msg


def test_the_preview_refuses_while_a_run_is_open():
    out = run(pr.preview(deps(FakeComet(listing()), Chain(), DB(live="RUN-1"))))
    assert not out["canExecute"] and any("RUN-1" in r for r in out["reasons"])


# --------------------------------------------------------------------------------------------- the execution
def test_both_pools_are_funded_quoted_swapped_back_to_back_and_read_from_celo():
    comet = FakeComet(listing())
    chain = Chain(receipts={"0xSWAP-USDT-IMC": {"amount": 1.27, "pair": "0xP2"}, "0xSWAP-USDC-IMC": {"amount": 1.79, "pair": "0xP1"}})
    db = DB()
    out = run(pr.execute(deps(comet, chain, db), requested_by="admin-1"))
    assert {s[0] for s in chain.sent} == {"USDT", "USDC"} and all(s[1] == ENGINE for s in chain.sent)   # funded up front
    calls = comet.calls
    assert calls[:2] == ["quote USDT->IMC", "quote USDC->IMC"]                     # every quote before any swap
    assert calls[2:4] == ["swap USDT->IMC", "swap USDC->IMC"]                      # then the swaps, back to back
    assert [s["confirmed"] for s in out["swaps"]] == [True, True] and out["swaps"][1]["received"] == pytest.approx(1.79)
    row = db["pool_rebalances"].rows[0]
    assert row["by"] == "admin-1" and len(row["fundTxs"]) == 2 and len(row["swaps"]) == 2


def test_only_the_shortfall_is_sent_and_nothing_when_the_wallet_already_holds_enough():
    comet, chain = FakeComet(listing()), Chain(engine={"USDC": 0.5, "USDT": 5.0})
    run(pr.execute(deps(comet, chain), requested_by="a"))
    assert [(s[0], round(s[2], 3)) for s in chain.sent] == [("USDC", round(0.9487 - 0.5, 3))]       # USDT needed no top-up


def test_without_a_readable_receipt_the_quote_is_used_and_marked_as_an_estimate():
    out = run(pr.execute(deps(FakeComet(listing()), Chain(engine={"USDC": 2.0, "USDT": 2.0})), requested_by="a"))
    assert [s["confirmed"] for s in out["swaps"]] == [False, False]


def test_a_quote_that_disagrees_with_the_plan_stops_everything_before_any_swap():
    comet, db = FakeComet(listing(), quote_scale=0.8), DB()
    with pytest.raises(pr.RebalanceError, match="does not match the plan"):
        run(pr.execute(deps(comet, Chain(engine={"USDC": 2.0, "USDT": 2.0}), db), requested_by="a"))
    assert not any(c.startswith("swap") for c in comet.calls)
    assert "does not match" in db["pool_rebalances"].rows[0]["error"]                     # the refusal is logged too


def test_if_the_second_swap_fails_the_message_says_the_first_went_through():
    comet = FakeComet(listing(), fail_pair=("USDC", "IMC"))
    db = DB()
    with pytest.raises(pr.RebalanceError) as e:
        run(pr.execute(deps(comet, Chain(engine={"USDC": 2.0, "USDT": 2.0}), db), requested_by="a"))
    assert "not lost" in str(e.value) and "IMC/USDT" in str(e.value) and "already went through" in str(e.value)
    assert len(db["pool_rebalances"].rows[0]["swaps"]) == 1


def test_funds_that_never_arrive_stop_before_any_quote_or_swap():
    comet = FakeComet(listing())
    with pytest.raises(pr.RebalanceError, match="not visible"):
        run(pr.execute(deps(comet, Chain(arrives=False)), requested_by="a"))
    assert comet.calls == []


def test_it_refuses_while_a_run_is_open_and_two_rebalances_cannot_overlap():
    with pytest.raises(pr.RebalanceError, match="RUN-9"):
        run(pr.execute(deps(FakeComet(listing()), Chain(), DB(live="RUN-9")), requested_by="a"))

    async def overlap():
        async with pr._lock:
            await pr.execute(deps(FakeComet(listing()), Chain()), requested_by="a")

    with pytest.raises(pr.RebalanceError, match="already in progress"):
        run(overlap())


# --------------------------------------------------------------------------------------------- the rounding bug of 2026-10-07
def test_amounts_are_whole_units_so_what_is_sent_always_covers_what_is_needed():
    p = pr.plan(pr.read_pools(listing(), CONTRACTS))
    for a in p["actions"]:
        assert round(a["spendAmount"] * 1e6, 6) == int(round(a["spendAmount"] * 1e6))        # a whole number of base units
    assert acts(p)["IMC/USDT"]["spendAmount"] == pytest.approx(0.676422, abs=1e-9)           # rounded UP from 0.67642156


def test_the_transfer_truncation_no_longer_leaves_the_engine_wallet_one_unit_short():
    """Before the fix: 0.67642156 was sent as 676421 units, the check wanted 676422, and the run stopped."""
    comet, chain = FakeComet(listing()), Chain()
    out = run(pr.execute(deps(comet, chain), requested_by="a"))
    held = {s: chain.balance(s, ENGINE) for s in ("USDT", "USDC")}
    assert held["USDT"] >= 0.676422 - 1e-12 and held["USDC"] >= 0.948714 - 1e-12
    assert [s["pair"] for s in out["swaps"]] == ["IMC/USDT", "IMC/USDC"]


def test_a_retry_after_a_one_unit_shortfall_tops_up_just_that_unit():
    comet, chain = FakeComet(listing()), Chain(engine={"USDT": 0.676421})                    # what the failed first attempt left behind
    run(pr.execute(deps(comet, chain), requested_by="a"))
    usdt_sends = [s for s in chain.sent if s[0] == "USDT"]
    assert len(usdt_sends) == 1 and usdt_sends[0][2] == pytest.approx(0.000001, abs=2e-9)
    assert chain.balance("USDT", ENGINE) == pytest.approx(0.676422)


def test_the_pool_run_funding_also_rounds_up_to_a_whole_unit(monkeypatch):
    import routes.swap_engine as se
    from Brain_Engine import state_engine
    seen = []

    async def fake_settle(address, asset, amount):
        seen.append(int(amount * 10 ** 6))                                                    # what the real transfer would send
        return {"success": True, "tx_hash": "0xT"}

    monkeypatch.setattr(se, "settle_crypto_on_celo", fake_settle)
    for shortfall in (0.0405, 0.040499999999, 0.676421999999, 0.363512):
        run(state_engine._real_fund_comet_wallet(ENGINE, shortfall))
    assert seen == [40500, 40500, 676422, 363512]


# --------------------------------------------------------------------------------------------- the background job
def _wait_for(job_id, timeout=5.0):
    async def go():
        for _ in range(int(timeout / 0.01)):
            j = pr.latest_job()
            if j["id"] == job_id and j["status"] != "running":
                return j
            await asyncio.sleep(0.01)
        raise AssertionError("the job never finished")
    return go()


def test_start_returns_at_once_and_the_job_reports_each_step_then_the_result():
    async def main():
        job = await pr.start(deps(FakeComet(listing()), Chain()), requested_by="admin-1")
        assert job["status"] == "running" and job["steps"] == [] and job["id"]            # returned before any work was done
        return await _wait_for(job["id"])

    done = run(main())
    assert done["status"] == "done" and [s["pair"] for s in done["result"]["swaps"]] == ["IMC/USDT", "IMC/USDC"]
    msgs = [s["msg"] for s in done["steps"]]
    assert msgs[0] == "Checking the pools and balances" and msgs[-1] == "Done"
    assert any(m.startswith("Sending") for m in msgs) and any("Swapping" in m and "IMC/USDT" in m for m in msgs)


def test_a_refusal_comes_back_immediately_and_starts_no_job():
    async def main():
        before = pr.latest_job()
        with pytest.raises(pr.RebalanceError, match="RUN-3"):
            await pr.start(deps(FakeComet(listing()), Chain(), DB(live="RUN-3")), requested_by="a")
        assert pr.latest_job() == before                                                  # nothing was started

    run(main())


def test_a_second_start_while_one_is_running_is_refused():
    async def main():
        first = await pr.start(deps(FakeComet(listing()), Chain()), requested_by="a")
        with pytest.raises(pr.RebalanceError, match="already in progress"):
            await pr.start(deps(FakeComet(listing()), Chain()), requested_by="a")
        await _wait_for(first["id"])

    run(main())


def test_a_job_that_fails_records_the_reason_and_frees_the_lock():
    async def main():
        job = await pr.start(deps(FakeComet(listing(), quote_scale=0.8), Chain(engine={"USDC": 2.0, "USDT": 2.0})), requested_by="a")
        failed = await _wait_for(job["id"])
        assert failed["status"] == "failed" and "does not match the plan" in failed["error"]
        nxt = await pr.start(deps(FakeComet(listing()), Chain(engine={"USDC": 2.0, "USDT": 2.0})), requested_by="a")   # the lock is free again
        return await _wait_for(nxt["id"])

    assert run(main())["status"] == "done"



def test_a_cancelled_run_does_not_block_the_rebalance():
    class CancelledRuns:
        async def find_one(self, q, proj=None):
            # a real database applies the cancelledAt filter: a cancelled run is not an open run
            return None if "cancelledAt" in q else {"_id": "RUN-STRAY"}

    out = run(pr.preview(deps(FakeComet(listing()), Chain(), {"corridor_runs": CancelledRuns(), "pool_rebalances": Audit()})))
    assert out["liveRun"] is None and out["canExecute"] is True
