"""The holdings view: every figure comes from an injected live source, a failing source is reported (never guessed), and the
sandbox gateway is kept out of the real total. No network."""
import asyncio

import pytest

from services import holdings as h

MASTER, ENGINE, AMM = "0xMASTER", "0xENGINE", "0xAMM"
CONTRACTS = {"IMC": "0xIMC", "USDC": "0xUSDC", "USDT": "0xUSDT", "cUSD": "0xCUSD"}


def run(coro):
    return asyncio.run(coro)


def deps(balances=None, lp=None, pools=None, float_kes=1711.5, gateway=None, sandbox=True, fail=(), usda=25.16, xlm=2.0, wallets=None):
    balances = balances or {}

    def token(sym, addr):
        if f"token:{sym}" in fail:
            raise RuntimeError("rpc down")
        return balances.get((sym, addr), 0.0)

    def pools_fn():
        return pools if pools is not None else {"status": "success", "data": {"pools": []}}

    def lp_fn(pair, wallet):
        return (lp or {}).get(pair, (0.0, 0.0))

    def float_fn():
        if "float" in fail:
            raise RuntimeError("ImpalaPay unreachable")
        return float_kes

    gw = gateway if gateway is not None else {"status": "success", "data": {"kesBalance": 460.0, "artmBalance": 18.0, "impaBalance": 300.0}}
    return h.Deps(wallets=wallets or [{"role": "master", "label": "Master wallet", "address": MASTER, "always": True},
                                      {"role": "engine", "label": "Engine wallet", "address": ENGINE, "always": False}],
                  contracts=CONTRACTS, token_balance_fn=token, celo_balance_fn=lambda a: {MASTER: 10.07, ENGINE: 0.21}.get(a, 0.0),
                  pools_fn=pools_fn, lp_fn=lp_fn, lp_wallet=AMM, float_fn=float_fn, gateway_fn=lambda: gw, gateway_is_sandbox=sandbox,
                  usda_fn=lambda: usda, xlm_fn=lambda: xlm)


def by_key(res):
    return {ln["key"]: ln for ln in res["lines"]}


def test_the_airtime_float_is_the_real_impalapay_balance():
    ln = by_key(run(h.gather(deps())))["AIRTIME_FLOAT"]
    assert ln["balance"] == 1711.5 and ln["group"] == "airtime" and ln["kes"] is True and ln["source"] == "live"


def test_wallet_balances_are_listed_per_wallet_and_token_with_gas_shown_but_not_valued():
    res = run(h.gather(deps({("USDT", MASTER): 0.104704, ("USDC", MASTER): 0.328136, ("IMC", MASTER): 2.844772, ("IMC", ENGINE): 2.513887, ("USDC", ENGINE): 0.030108})))
    k = by_key(res)
    assert k["master:IMC"]["balance"] == pytest.approx(2.844772) and k["master:IMC"]["usd"] == pytest.approx(2.844772) and k["master:IMC"]["group"] == "imc"
    assert k["master:USDT"]["group"] == "stable" and k["master:USDC"]["usd"] == pytest.approx(0.328136)
    assert k["engine:IMC"]["balance"] == pytest.approx(2.513887) and "engine:USDT" not in k          # zero balances in a non-main wallet are left out
    assert k["master:CELO"]["usd"] is None and k["master:CELO"]["group"] == "other"                  # gas is shown, not valued
    assert "master:cUSD" not in k or k["master:cUSD"]["balance"] == 0.0


def test_the_main_wallet_always_shows_even_when_a_balance_is_zero():
    k = by_key(run(h.gather(deps())))
    assert k["master:USDT"]["balance"] == 0.0 and k["master:cUSD"]["balance"] == 0.0


def test_our_liquidity_is_our_share_of_each_pool():
    pools = {"status": "success", "data": {"pools": [
        {"token0": "0xIMC", "token1": "0xUSDC", "pair": "0xP1", "reserve0": "2344000", "reserve1": "1715000"},
        {"token0": "0xUSDT", "token1": "0xIMC", "pair": "0xP2", "reserve0": "1237500", "reserve1": "1643600"},
        {"token0": "0xUSDT", "token1": "0xUSDC", "pair": "0xP3", "reserve0": "0", "reserve1": "0"}]}}
    lp = {"0xP1": (1.9990, 2.0), "0xP2": (1.4237, 1.4244)}
    res = run(h.gather(deps(pools=pools, lp=lp)))
    k = by_key(res)
    p1 = k["POOL:IMC/USDC"]
    assert p1["group"] == "pool" and p1["detail"]["share"] == pytest.approx(0.9995)
    assert p1["detail"]["amounts"]["IMC"] == pytest.approx(2.344 * 0.9995, abs=1e-6) and p1["detail"]["amounts"]["USDC"] == pytest.approx(1.715 * 0.9995, abs=1e-6)
    assert p1["usd"] == pytest.approx((2.344 + 1.715) * 0.9995, abs=1e-6)                                # stable + IMC at face
    assert p1["detail"]["price"] == pytest.approx(1.715 / 2.344, abs=1e-6)
    assert "POOL:USDT/IMC" in k and "POOL:USDT/USDC" not in k                                            # an empty pool is not ours


def test_a_pool_we_hold_no_lp_tokens_in_is_not_listed():
    pools = {"status": "success", "data": {"pools": [{"token0": "0xIMC", "token1": "0xUSDC", "pair": "0xP1", "reserve0": "2000000", "reserve1": "2000000"}]}}
    assert "POOL:IMC/USDC" not in by_key(run(h.gather(deps(pools=pools, lp={"0xP1": (0.0, 2.0)}))))


def test_a_sandbox_gateway_is_kept_out_of_the_real_groups_and_labelled():
    res = run(h.gather(deps(sandbox=True)))
    gw = [ln for ln in res["lines"] if ln["key"].startswith("GATEWAY:")]
    assert len(gw) == 3 and all(ln["group"] == "sandbox" and ln["source"] == "sandbox" for ln in gw) and res["gatewaySandbox"] is True
    real_groups = {ln["group"] for ln in res["lines"] if ln["source"] == "live"}
    assert "sandbox" not in real_groups


def test_a_live_gateway_counts_as_real_money():
    res = run(h.gather(deps(sandbox=False)))
    k = by_key(res)
    assert k["GATEWAY:kesBalance"]["group"] == "mobile" and k["GATEWAY:kesBalance"]["source"] == "live" and k["GATEWAY:kesBalance"]["kes"] is True
    assert k["GATEWAY:impaBalance"]["group"] == "imc" and k["GATEWAY:impaBalance"]["usd"] == 300.0


def test_a_failing_source_is_reported_not_guessed_and_the_rest_still_load():
    res = run(h.gather(deps({("IMC", MASTER): 2.84}, fail=("float",))))
    assert "AIRTIME_FLOAT" not in by_key(res)                                                            # no stale or zero figure invented
    assert any(e["source"] == "ImpalaPay airtime float" and "unreachable" in e["message"] for e in res["errors"])
    assert by_key(res)["master:IMC"]["balance"] == 2.84


def test_a_failing_token_read_is_reported_per_token():
    res = run(h.gather(deps({("IMC", MASTER): 2.84}, fail=("token:USDT",))))
    assert any("USDT" in e["source"] for e in res["errors"]) and "master:USDT" not in by_key(res)
    assert by_key(res)["master:IMC"]["balance"] == 2.84


def test_a_gateway_error_is_reported():
    res = run(h.gather(deps(gateway={"status": "error", "message": "Authentication failed"})))
    assert not any(ln["key"].startswith("GATEWAY:") for ln in res["lines"])
    assert any(e["source"] == "Mam-laka gateway" and "Authentication failed" in e["message"] for e in res["errors"])


def test_non_operators_see_the_figures_without_wallet_addresses():
    res = run(h.gather(deps({("IMC", MASTER): 2.84})))
    assert by_key(res)["master:IMC"]["address"] == MASTER
    stripped = h.strip_addresses(res)
    assert all(ln["address"] is None for ln in stripped["lines"])
    assert by_key(stripped)["master:IMC"]["balance"] == 2.84                                              # same numbers


def test_cardano_and_stellar_vaults_are_included_when_read():
    k = by_key(run(h.gather(deps())))
    assert k["N7_USDA"]["balance"] == 25.16 and k["N7_USDA"]["group"] == "stable"
    assert k["N9_XLM"]["usd"] is None
