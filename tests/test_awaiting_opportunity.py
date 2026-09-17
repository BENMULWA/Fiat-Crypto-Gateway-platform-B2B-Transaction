"""ROLLOVER no longer loops straight back to PROCURE. Below max_cycles it
hands off to AWAITING_OPPORTUNITY, which only starts the next cycle once
find_best_open_opportunity() actually has a ranked, open corridor to route
through — holding (and re-polling) otherwise. At max_cycles, ROLLOVER goes
straight to CELO_EXIT as before, unaffected by this gate."""
import asyncio

import pytest

from Brain_Engine.node_registry import CORRIDORS
from Brain_Engine.state_engine import HFTCorridorFSM, ImmutableLedger, FSMState
from Brain_Engine import state_engine as state_engine_module


def _build_fsm(max_cycles=5, current_cycle=1):
    ledger = ImmutableLedger(db_collection=None)
    fsm = HFTCorridorFSM(
        ledger,
        starting_capital_usd=100 / 129.50,
        config={
            "node_procure": "N2", "node_liquidate": "N5",
            "discount": 0.06, "baseline_rate": 129.50,
            "cycles": max_cycles, "run_id": "AO-TEST",
        },
    )
    fsm.current_cycle = current_cycle
    return ledger, fsm


def test_rollover_holds_in_awaiting_opportunity_below_max_cycles():
    _, fsm = _build_fsm(max_cycles=5, current_cycle=1)
    asyncio.run(fsm._evaluate_rollover())
    assert fsm.state == FSMState.AWAITING_OPPORTUNITY
    assert fsm.current_cycle == 1  # unchanged — only AWAITING_OPPORTUNITY advances it, on a real opportunity


def test_rollover_goes_straight_to_celo_exit_at_max_cycles():
    _, fsm = _build_fsm(max_cycles=5, current_cycle=5)
    asyncio.run(fsm._evaluate_rollover())
    assert fsm.state == FSMState.CELO_EXIT


def test_awaiting_opportunity_holds_and_repolls_when_nothing_is_open(monkeypatch):
    """No eligible corridor -> stays in AWAITING_OPPORTUNITY, principal and
    cycle both untouched, and it doesn't block forever — it sleeps a
    bounded poll interval so a caller's tick loop keeps making progress."""
    monkeypatch.setattr(state_engine_module, "find_best_open_opportunity", lambda: None)
    monkeypatch.setattr(state_engine_module, "AWAITING_OPPORTUNITY_POLL_SECONDS", 0.01)

    _, fsm = _build_fsm(max_cycles=5, current_cycle=2)
    fsm.state = FSMState.AWAITING_OPPORTUNITY
    principal_before = fsm.current_usd_principal

    asyncio.run(fsm._evaluate_awaiting_opportunity())

    assert fsm.state == FSMState.AWAITING_OPPORTUNITY  # still holding
    assert fsm.current_cycle == 2  # not advanced
    assert fsm.current_usd_principal == pytest.approx(principal_before)  # compounding untouched by the hold


def test_awaiting_opportunity_advances_to_next_cycle_when_one_opens(monkeypatch):
    """An open opportunity -> advance to PROCURE with the SAME USDA
    principal cycle 1 finished with (no fresh capital), cycle count
    incremented, and the FSM's routing switched to whichever corridor
    actually won the ranking (may differ from where this run started)."""
    fake_winner = {
        "id": "telkom_5x", "node_procure": "N1", "node_liquidate": "N4",
        "discount": 0.10, "fx_edge": 0.05, "name": "TELKOM",
    }
    monkeypatch.setattr(state_engine_module, "find_best_open_opportunity", lambda: fake_winner)

    _, fsm = _build_fsm(max_cycles=5, current_cycle=2)
    fsm.state = FSMState.AWAITING_OPPORTUNITY
    fsm.current_usd_principal = 0.1234  # what the prior cycle's MINT left behind
    principal_before = fsm.current_usd_principal

    asyncio.run(fsm._evaluate_awaiting_opportunity())

    assert fsm.state == FSMState.PROCURE
    assert fsm.current_cycle == 3
    assert fsm.current_usd_principal == pytest.approx(principal_before)  # carried forward, not reset
    assert fsm.NODE_PROCURE == "N1"
    assert fsm.NODE_LIQ == "N4"
    assert fsm.DISCOUNT == 0.10
    assert fsm.FX_EDGE == 0.05


def test_find_best_open_opportunity_picks_the_only_live_corridor_today():
    """Only airtel_5x is actually eligible right now — telkom_5x's nodes
    (N1 Telkom, N4 T-Kash) are both live=False in node_registry.py, so
    corridor_eligible("telkom_5x") is False regardless of its better
    on-paper yield. This pins that real-world state rather than assuming
    both corridors compete."""
    assert set(CORRIDORS) == {"airtel_5x", "telkom_5x"}  # pin the assumption this test relies on

    winner = state_engine_module.find_best_open_opportunity()
    assert winner is not None
    assert winner["id"] == "airtel_5x"


def test_find_best_open_opportunity_ranks_by_projected_yield_once_both_are_live(monkeypatch):
    """With both corridors eligible, the higher-yield one (telkom_5x, 10%
    discount + 5% fx edge) must rank above airtel_5x (6%, 0% edge) — this
    is the ranking logic in isolation, independent of which nodes happen
    to be live today."""
    monkeypatch.setattr(state_engine_module, "corridor_eligible", lambda corridor_id: True)

    winner = state_engine_module.find_best_open_opportunity()
    assert winner is not None
    assert winner["id"] == "telkom_5x"


def test_find_best_open_opportunity_returns_none_when_nothing_eligible(monkeypatch):
    monkeypatch.setattr(state_engine_module, "corridor_eligible", lambda corridor_id: False)
    assert state_engine_module.find_best_open_opportunity() is None
