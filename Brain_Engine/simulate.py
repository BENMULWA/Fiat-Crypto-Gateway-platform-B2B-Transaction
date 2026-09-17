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


async def _fake_celo_swap(usda_amount: float) -> str:
    # 66-char 0x-prefixed hex, shaped exactly like a real Celo tx hash —
    # but SIMULATED_TX_HASH below is the actual signal the UI/caller must
    # key off to avoid ever mistaking this for a genuine broadcast.
    return "0x" + secrets.token_hex(32)


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

    bot = HFTCorridorFSM(
        ledger,
        starting_capital_usd=principal_usd,
        config={
            "cycles": 5,
            "discount": 0.06,
            "fx_edge": 0.0,
            "node_procure": "N2",
            "node_liquidate": "N5",
            "simulate": True,
            "poll_seconds": 0.05,
            "send_airtime_fn": _fake_send_airtime,
            "get_merchant_balance_fn": _fake_get_merchant_balance,
            "get_vault_balance_fn": _fake_get_vault_balance,
            "celo_swap_fn": _fake_celo_swap,
            "find_opportunity_fn": lambda: finder(cycle_box),
        },
    )

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
