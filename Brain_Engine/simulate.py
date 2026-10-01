"""Runs the REAL corridor state machine (Brain_Engine/state_engine.py's
HFTCorridorFSM) end to end with every external dependency swapped for a
deterministic fake — no real airtime purchase, no real paybill balance
read, no real Cardano vault check, no real Celo broadcast. This exercises
the exact PROCURE -> LIQUIDATE -> MINT -> AWAITING_OPPORTUNITY -> ...
-> CELO_EXIT state machine real traffic uses (same math, same gates, same
class), just with injected fakes instead of live network calls — so a
demo genuinely proves the state machine works, not a separate
reimplementation of the same formulas that could silently drift from it.

Fakes are passed in via HFTCorridorFSM's constructor config
(send_airtime_fn / get_merchant_balance_fn / get_vault_balance_fn /
celo_swap_fn / find_opportunity_fn), not monkeypatched onto the shared
module-level singletons — mutating those in place would be a real
concurrency hazard the moment a simulated run and a real one are ever in
flight on the same server at the same time.
"""
from __future__ import annotations

import secrets
import uuid
from typing import Any, Optional

from Brain_Engine.node_registry import CORRIDORS, corridor_eligible
from Brain_Engine.Discovery_Engine import IMMDiscoveryEngine
from services.comet_client import CometClient
from Brain_Engine.state_engine import (
    HFTCorridorFSM,
    ImmutableLedger,
    FSMState,
)

_discovery_engine = IMMDiscoveryEngine()


def _fake_send_airtime(phone: str, amount_kes: int, reference: str) -> dict[str, Any]:
    return {
        "status": "success",
        "receipt_id": f"SIM-{uuid.uuid4().hex[:10].upper()}",
        "request_ref": reference,
        "provider_transaction_id": None,
        "wallet_balance": None,
    }


def _fake_get_merchant_balance() -> dict[str, Any]:
    # Deliberately generous so a demo run is never blocked by float —
    # the point of this simulation is to show the state machine's shape,
    # not to model provider inventory limits (the live 5 KES test already
    # proved those are real and do halt the FSM correctly).
    return {"status": "success", "data": {"kesBalance": 10_000_000.0, "artmBalance": 10_000_000.0}}


def _fake_get_vault_balance() -> float:
    return 10_000_000.0


def _make_fake_impala_provider(discount: float):
    """Returns (get_payout_balance_fn, topup_via_stk_fn) sharing mutable
    state, so _execute_procure's real poll-for-balance-increase loop (it
    polls services.impala_airtime.get_payout_balance for the real delta a
    real STK top-up produces) sees a genuine increase right after the fake
    topup call — same shape as the real confirmation flow, just instant
    instead of asynchronous. Starts deliberately generous (10,000,000) so
    check_airtime_backing never blocks a demo run on inventory limits —
    same convention as _fake_get_merchant_balance above.

    The linear markup here (amount * (1 + discount)) is the real, confirmed
    formula (a live 500 KES top-up returned 525 KES of float, exactly
    ×1.05) — not the inverse-discount formula used before that evidence
    existed."""
    state = {"balance": 10_000_000.0}

    def fake_get_payout_balance() -> dict[str, Any]:
        return {"artm_balance": state["balance"], "currency": "KES", "raw": {}}

    def fake_topup_via_stk(amount_kes: int, paying_phone_number: str) -> dict[str, Any]:
        state["balance"] += amount_kes * (1 + discount)
        return {"status": "success"}

    return fake_get_payout_balance, fake_topup_via_stk


async def _fake_celo_swap(usda_amount: float) -> str:
    # 66-char 0x-prefixed hex, shaped exactly like a real Celo tx hash —
    # but SIMULATED_TX_HASH below is the actual signal the UI/caller must
    # key off to avoid ever mistaking this for a genuine broadcast.
    return "0x" + secrets.token_hex(32)


async def _fake_get_swap_rate(from_asset: str, to_asset: str) -> float:
    return 1.0


def _fake_get_treasury_usdt_balance() -> float:
    return 1_000_000.0


async def _fake_celo_transfer(token_address: str, to_address: str, amount: float, decimals: int = 6) -> str:
    # Stands in for celo_integrations.corridor_api.transfer_erc20 so a
    # simulated mint_provider="comet" run never broadcasts a real
    # transaction while depositing minted IMC into the (fake) Comet wallet.
    return "0x" + secrets.token_hex(32)


class _FakeCometClient(CometClient):
    """Stands in for services.comet_client.CometClient on a simulated run.
    Subclasses the real client purely to inherit to_base_units/
    from_base_units (pure unit-conversion helpers, no network call) instead
    of duplicating them — every method that actually hits the network
    (tokenize_airtime, execute_amm_swap, get_amm_quote) is overridden below
    to never make a real HTTP request. __init__ is never called (no env
    vars read, no credentials needed) since this class sets its own state.

    Without this, a simulated run of a mint_provider="comet" corridor would
    silently fall back to the real module-level `comet_client` singleton
    and make real Comet API calls (tokenize_airtime, execute_amm_swap)
    under the "Simulate 5x" button — exactly the kind of demo bug worth
    catching before it ships.

    base_rate is passed in from the same value the FSM itself is
    configured with (not a second hardcoded copy) purely so amountOut is
    denominated correctly (KES vs. USD-ish) for the trace to read sensibly
    — it does not model any AMM slippage or Comet's real pool pricing,
    since every corridor that uses this fake today has fx_edge=0.0; the
    only profit this simulation is meant to demonstrate is the airtime
    discount already captured before this call runs."""

    def __init__(self, base_rate: float):  # noqa: super().__init__ deliberately skipped — see docstring
        self.base_rate = base_rate

    def _convert(self, from_symbol: str, to_symbol: str, amount: float) -> float:
        if from_symbol == "KES" and to_symbol in ("IMC", "USDC"):
            return amount / self.base_rate
        if from_symbol in ("IMC", "USDC") and to_symbol == "KES":
            return amount * self.base_rate
        return amount  # stablecoin<->stablecoin, e.g. IMC->USDC

    def tokenize_airtime(self, external_user_id, amount_base, external_id, chain="celo"):
        return {
            "status": "success",
            "data": {
                "status": "success", "asset": "IMC", "type": "airtime", "chain": chain,
                "externalUserId": external_user_id, "to": "0xSIMULATED", "amountBase": amount_base,
                "txHash": "0xSIMULATED" + secrets.token_hex(28),
            },
        }

    def execute_amm_swap(self, external_user_id, from_symbol, to_symbol, amount_in, tenant_slug=None):
        # Real execute response has no amountOut field (see docstring) —
        # the caller reads the settled amount from the pre-flight quote
        # instead, so this fake doesn't need to compute one either.
        return {
            "status": "success",
            "data": {
                "status": "success", "from": from_symbol, "to": to_symbol,
                "amountIn": amount_in, "txHash": "0xSIMULATED" + secrets.token_hex(28), "chainId": 42220,
            },
        }

    def get_amm_quote(self, from_symbol, to_symbol, amount_in):
        amount_out = self._convert(from_symbol, to_symbol, self.from_base_units(amount_in))
        return {"status": "success", "data": {"from": from_symbol, "to": to_symbol, "amountOut": self.to_base_units(amount_out)}}

    # IMM endpoints (used only by _execute_comet_exit's reseed leg, when
    # floor_kes > 0 — not exercised by run_simulated_5x_cycle today, but
    # overridden anyway so a future simulated run that does set floor_kes
    # can't fall through to the real module-level CometClient singleton.
    # Note: plain float amounts here, not base-unit strings — see
    # comet_client.py's get_imm_quote/execute_imm_swap docstrings.
    def get_imm_quote(self, base, quote, amount_in):
        return {"status": "success", "data": {"amountOut": self._convert(base, quote, amount_in)}}

    def execute_imm_swap(self, external_user_id, chain, base, quote, amount_in, external_id):
        return {
            "status": "success",
            "data": {"amountOut": self._convert(base, quote, amount_in), "vaultBacked": True, "txHash": "0xSIMULATED" + secrets.token_hex(28)},
        }

    def get_or_create_wallet(self, external_user_id, chain="celo"):
        return {"status": "success", "data": {"address": "0xSIMULATEDCOMETWALLET0000000000000000001"}}

    def send_asset(self, symbol, external_user_id, to, amount_base):
        return {
            "status": "success",
            "data": {"txHash": "0xSIMULATED" + secrets.token_hex(28), "chain": "celo", "symbol": symbol, "amountBase": amount_base},
        }


def _make_opportunity_finder(mocked_node_ids: Optional[set[str]], hold_plan: dict[int, int]):
    """Builds the find_opportunity_fn injected into a simulated FSM.

    mocked_node_ids: if given, a corridor is treated as eligible when both
    its procure/liquidate nodes are in this set — regardless of their real
    node_registry.py `live` flag. This is what makes "mocked nodes"
    demonstrable: e.g. include N1/N4 to show the Telkom corridor winning
    a cycle even though it has no real integration live today.
    If None, real corridor_eligible() decides (today: only airtel_5x).

    hold_plan: {cycle_number: holds_remaining} — lets a specific cycle
    visibly hold in AWAITING_OPPORTUNITY for N poll ticks before an
    opportunity "opens", purely to demonstrate that gate exists. Mutated
    in place as holds are consumed.
    """
    def eligible(corridor_id: str) -> bool:
        if mocked_node_ids is None:
            return corridor_eligible(corridor_id)
        corridor = CORRIDORS[corridor_id]
        return corridor["node_procure"] in mocked_node_ids and corridor["node_liquidate"] in mocked_node_ids

    def find_best_open_opportunity(current_cycle: list[int]) -> Optional[dict]:
        cycle = current_cycle[0]
        remaining = hold_plan.get(cycle, 0)
        if remaining > 0:
            hold_plan[cycle] = remaining - 1
            return None

        open_corridors = [
            {"id": corridor_id, **corridor}
            for corridor_id, corridor in CORRIDORS.items()
            if eligible(corridor_id)
        ]
        if not open_corridors:
            return None
        ranked = sorted(
            open_corridors,
            key=lambda c: _discovery_engine.project_corridor_yield(
                discount_rate=c["discount"], fx_edge_pct=c["fx_edge"], cycles=1
            )["single_cycle_multiplier"],
            reverse=True,
        )
        return ranked[0]

    return find_best_open_opportunity


async def run_simulated_5x_cycle(
    principal_usd: float,
    mocked_node_ids: Optional[list[str]] = None,
    hold_cycles: Optional[list[int]] = None,
    corridor_id: Optional[str] = None,
) -> dict[str, Any]:
    """Drives a full simulated corridor run and returns a step-by-step
    trace for the UI to render/animate.

    mocked_node_ids: node ids to treat as "live" for this simulation only
    (e.g. ["N1","N2","N4","N5"] to light up both Airtel and Telkom
    corridors even though only Airtel is really wired today). None means
    use real corridor eligibility.

    hold_cycles: which upcoming cycle numbers should visibly hold in
    AWAITING_OPPORTUNITY for a couple of poll ticks before resolving, to
    demonstrate the hold/poll gate on screen. Defaults to holding cycle 2.

    corridor_id: which node_registry.CORRIDORS entry to simulate — None
    keeps every existing caller's exact prior behavior (hardcoded
    N2/N5-shaped config: 6% discount, 0% fx_edge, Cardano/native-Celo
    fakes). Passing e.g. "airtel_5x" (mint_provider="comet" since
    2026-09-29) reads that corridor's own discount/fx_edge/mint_provider/
    exit_provider and injects _FakeCometClient instead — without this,
    simulating a mint_provider="comet" corridor would silently fall
    through to the real Comet client.
    """
    hold_cycles = hold_cycles if hold_cycles is not None else [2]
    hold_plan = {c: 2 for c in hold_cycles}  # 2 poll ticks of "nothing open yet" per named cycle
    node_set = set(mocked_node_ids) if mocked_node_ids else None

    ledger = ImmutableLedger(db_collection=None)

    # current_cycle is read by the opportunity finder via a 1-element list
    # (closed over by reference) since the finder is built before the FSM
    # instance exists — simplest way to give it live access to
    # bot.current_cycle without restructuring HFTCorridorFSM's signature.
    cycle_box = [1]
    finder = _make_opportunity_finder(node_set, hold_plan)

    corridor = CORRIDORS.get(corridor_id, CORRIDORS["airtel_5x"]) if corridor_id else {
        "node_procure": "N2", "node_liquidate": "N5", "discount": 0.06, "fx_edge": 0.0,
    }
    base_rate = 129.50
    fake_get_payout_balance, fake_topup_via_stk = _make_fake_impala_provider(corridor["discount"])

    config: dict[str, Any] = {
        "cycles": 5,
        "discount": corridor["discount"],
        "fx_edge": corridor["fx_edge"],
        "node_procure": corridor["node_procure"],
        "node_liquidate": corridor["node_liquidate"],
        "baseline_rate": base_rate,
        "simulate": True,
        "poll_seconds": 0.05,
        "stk_poll_interval_seconds": 0.01,
        "stk_poll_attempts": 3,
        "stk_paying_phone": "0700000000",
        "send_airtime_fn": _fake_send_airtime,
        "get_merchant_balance_fn": _fake_get_merchant_balance,
        "get_vault_balance_fn": _fake_get_vault_balance,
        "get_payout_balance_fn": fake_get_payout_balance,
        "topup_via_stk_fn": fake_topup_via_stk,
        "celo_swap_fn": _fake_celo_swap,
        "celo_transfer_fn": _fake_celo_transfer,
        # Fixed 1:1 fake for the internal IMC->USDT rate lookup
        # (state_engine._execute_mint_comet no longer calls Comet's IMM —
        # see that method's docstring) and a fake treasury USDT balance
        # comfortably above anything a simulated run mints, so neither
        # touches the real rate book or a real Celo RPC call.
        "get_swap_rate_fn": _fake_get_swap_rate,
        "get_treasury_usdt_balance_fn": _fake_get_treasury_usdt_balance,
        "find_opportunity_fn": lambda: finder(cycle_box),
    }
    if corridor.get("mint_provider"):
        config["mint_provider"] = corridor["mint_provider"]
    if corridor.get("exit_provider"):
        config["exit_provider"] = corridor["exit_provider"]
    if corridor.get("mint_provider") == "comet" or corridor.get("exit_provider") == "comet":
        config["comet_client"] = _FakeCometClient(base_rate=base_rate)
        config["comet_user_id"] = 999999  # obviously-fake placeholder; a real run reads COMET_TREASURY_USER_ID

    bot = HFTCorridorFSM(ledger, starting_capital_usd=principal_usd, config=config)

    steps: list[dict[str, Any]] = []
    await bot.boot_system()

    seen = 0
    ticks = 0
    max_ticks = 500  # safety valve against an unexpected infinite hold
    while bot.state not in (FSMState.COMPLETED, FSMState.HALTED) and ticks < max_ticks:
        state_before = bot.state
        cycle_box[0] = bot.current_cycle
        await bot.tick()
        ticks += 1

        new_entries = ledger.local_records[seen:]
        seen = len(ledger.local_records)
        for entry in new_entries:
            steps.append({
                "cycle": entry.cycle,
                "type": entry.txn_type.value,
                "from": entry.from_node,
                "to": entry.to_node,
                "asset": entry.asset,
                "amount": entry.amount,
                "externalRef": entry.external_ref,
            })

        if bot.state != state_before:
            steps.append({
                "cycle": bot.current_cycle,
                "type": "STATE_CHANGE",
                "from": state_before.value,
                "to": bot.state.value,
                "asset": None,
                "amount": bot.current_usd_principal,
                "externalRef": None,
            })
        elif bot.state == FSMState.AWAITING_OPPORTUNITY:
            steps.append({
                "cycle": bot.current_cycle,
                "type": "HOLDING",
                "from": "AWAITING_OPPORTUNITY",
                "to": "AWAITING_OPPORTUNITY",
                "asset": None,
                "amount": bot.current_usd_principal,
                "externalRef": None,
            })

    return {
        "simulated": True,
        "status": "success" if bot.state == FSMState.COMPLETED else "halted",
        "finalState": bot.state.value,
        "startingUsd": principal_usd,
        "finalUsd": bot.current_usd_principal,
        "profit": bot.current_usd_principal - principal_usd,
        "cyclesReached": bot.current_cycle,
        "steps": steps,
    }
