"""Tests for the 5% reseller top-up discount: a real STK top-up of 500 KES
returned 525 KES of float — exactly amount * (1 + 0.05), the LINEAR
markup — confirmed live 2026-09-25 against the real ImpalaPay Reseller API
(airtime-api.impalapay.com). Not the inverse-discount formula
(amount / (1 - discount)) this corridor used before that evidence existed.

Covers the same math from two angles:
 - the standalone formula, so the 525-for-500 example stays pinned
 - the live PROCURE state (Brain_Engine/state_engine.py), which now tops
   up via a real STK push (services.impala_airtime.topup_via_stk) and
   polls the real payout balance for the confirmed delta — not
   send_airtime, which only ever debits an already-funded float 1:1.
"""
import asyncio

import pytest

from Brain_Engine.state_engine import HFTCorridorFSM, ImmutableLedger, FSMState
from Brain_Engine import state_engine as state_engine_module


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(self._payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")

    def json(self):
        return self._payload


def test_525_kes_float_for_500_kes_topup():
    """Pins the exact real example: 500 KES topped up at a 5% reseller
    discount returns 525 KES of float — linear markup, not inverse."""
    amount_paid_kes = 500
    discount = 0.05

    float_credited_kes = amount_paid_kes * (1 + discount)

    assert float_credited_kes == pytest.approx(525.0, abs=0.001)


def _mock_impala_reseller(monkeypatch, topup_ok: bool, discount: float = 0.05):
    """Fakes the real ImpalaPay Reseller API for a PROCURE run: POST
    /api/app/topup (the STK push) and GET /api/app/details (the balance
    _execute_procure polls before and after to confirm the top-up landed).
    Shared mutable state so the balance genuinely increases by the linear
    markup right after a successful topup call, matching the real
    confirmation flow's shape."""
    state = {"balance": 1000.0}

    def fake_post(url, **kwargs):
        if url.endswith("/api/app/topup"):
            if not topup_ok:
                return FakeResponse(400, {"error": True, "message": "Insufficient balance"}, text="Insufficient balance")
            amount = kwargs.get("json", {}).get("amount", 0)
            state["balance"] += amount * (1 + discount)
            return FakeResponse(200, {"status": "success"})
        raise AssertionError(f"Unexpected POST: {url}")

    def fake_get(url, **kwargs):
        if url.endswith("/api/app/details"):
            return FakeResponse(200, {"accountBalance": state["balance"]})
        raise AssertionError(f"Unexpected GET: {url}")

    monkeypatch.setattr("services.impala_airtime.requests.post", fake_post)
    monkeypatch.setattr("services.impala_airtime.requests.get", fake_get)
    # A cached session token sidesteps login() entirely — get_payout_balance
    # and topup_via_stk both only need the session token, not login itself.
    from services.impala_airtime import ImpalaAirtimeClient
    client = ImpalaAirtimeClient()
    client._session_token = "test-session-token"
    monkeypatch.setattr(state_engine_module, "impala_airtime", client)
    monkeypatch.setitem(state_engine_module.PROCUREMENT_WALLETS, "N2", "0733253036")
    return state


def test_procure_state_tops_up_via_stk_at_5pct_discount(monkeypatch):
    """Drives the FSM's real PROCURE state for a 500 KES top-up and
    confirms it hands off to LIQUIDATE with the real, polled balance delta
    as the captured float — not a formula-computed guess."""
    state = _mock_impala_reseller(monkeypatch, topup_ok=True, discount=0.05)

    base_rate = 129.50
    discount = 0.05
    amount_to_pay_kes = 500
    principal_usd = amount_to_pay_kes / base_rate

    ledger = ImmutableLedger(db_collection=None)
    fsm = HFTCorridorFSM(
        ledger,
        starting_capital_usd=principal_usd,
        config={
            "node_procure": "N2",
            "discount": discount,
            "baseline_rate": base_rate,
            "run_id": "TEST-RUN",
            "stk_poll_interval_seconds": 0.01,
            "stk_poll_attempts": 3,
        },
    )

    asyncio.run(fsm._execute_procure())

    assert fsm.state == FSMState.LIQUIDATE, f"PROCURE should succeed and hand off to LIQUIDATE (halted: {fsm.halt_reason})"

    procure_entries = [e for e in ledger.local_records if e.txn_type.value == "PROCURE"]
    assert len(procure_entries) == 1
    assert procure_entries[0].amount == pytest.approx(525.0, abs=0.5)  # 500 * 1.05, the real confirmed delta
    assert fsm.current_kes_float == pytest.approx(525.0, abs=0.5)


def test_procure_state_pauses_for_manual_topup_when_provider_rejects(monkeypatch):
    """If the STK top-up itself is rejected outright (real documented
    shape: 400 "Insufficient balance", or a misconfigured non-Safaricom
    phone number — see PROCUREMENT_WALLET_N2's real 2026-09-29 incident),
    the FSM must pause into AWAITING_MANUAL_TOPUP rather than halt or
    silently credit airtime that was never actually bought — a manual
    top-up (or a fixed retry) can still land and be detected."""
    _mock_impala_reseller(monkeypatch, topup_ok=False)

    ledger = ImmutableLedger(db_collection=None)
    fsm = HFTCorridorFSM(
        ledger,
        starting_capital_usd=500 / 129.50,
        config={"node_procure": "N2", "discount": 0.05, "baseline_rate": 129.50, "run_id": "TEST-RUN-2"},
    )

    asyncio.run(fsm._execute_procure())

    assert fsm.state == FSMState.AWAITING_MANUAL_TOPUP
    assert fsm.pending_topup_expected_kes == pytest.approx(500.0, abs=0.5)
    assert not any(e.txn_type.value == "PROCURE" for e in ledger.local_records)


def test_procure_state_pauses_for_manual_topup_when_stk_never_confirms(monkeypatch):
    """An STK push can be accepted by the provider but never actually
    approved (prompt ignored/timed out on the paying phone) — the real
    balance never increases within the short poll window. PROCURE must
    pause into AWAITING_MANUAL_TOPUP rather than assume success from the
    topup call alone or give up outright — approving the prompt late (or a
    manual top-up) can still be detected and continue the run."""
    def fake_post(url, **kwargs):
        if url.endswith("/api/app/topup"):
            return FakeResponse(200, {"status": "success"})  # accepted, but never actually approved
        raise AssertionError(f"Unexpected POST: {url}")

    def fake_get(url, **kwargs):
        if url.endswith("/api/app/details"):
            return FakeResponse(200, {"accountBalance": 1000.0})  # never moves
        raise AssertionError(f"Unexpected GET: {url}")

    monkeypatch.setattr("services.impala_airtime.requests.post", fake_post)
    monkeypatch.setattr("services.impala_airtime.requests.get", fake_get)
    from services.impala_airtime import ImpalaAirtimeClient
    client = ImpalaAirtimeClient()
    client._session_token = "test-session-token"
    monkeypatch.setattr(state_engine_module, "impala_airtime", client)
    monkeypatch.setitem(state_engine_module.PROCUREMENT_WALLETS, "N2", "0733253036")

    ledger = ImmutableLedger(db_collection=None)
    fsm = HFTCorridorFSM(
        ledger,
        starting_capital_usd=500 / 129.50,
        config={
            "node_procure": "N2", "discount": 0.05, "baseline_rate": 129.50, "run_id": "TEST-RUN-3",
            "stk_poll_interval_seconds": 0.01, "stk_poll_attempts": 2,
        },
    )

    asyncio.run(fsm._execute_procure())

    assert fsm.state == FSMState.AWAITING_MANUAL_TOPUP
    assert fsm.pending_topup_expected_kes == pytest.approx(500.0, abs=0.5)
    assert not any(e.txn_type.value == "PROCURE" for e in ledger.local_records)
