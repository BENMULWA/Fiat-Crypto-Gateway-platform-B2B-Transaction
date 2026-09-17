"""Tests for the 6% wholesale airtime-procurement discount: for every 1 KES
of float spent, the provider disburses 1 KES / (1 - 0.06) ≈ 1.0638 KES of
physical airtime (100 KES face value -> 106 KES of airtime, matching the
reseller agreement this corridor implements).

Covers the same math from two angles:
 - the standalone formula, so the 106-for-100 example stays pinned
 - the live PROCURE state (Brain_Engine/state_engine.py) that actually
   calls the provider, with the network call faked at the requests layer
   (same technique as tests/test_impala_airtime.py) so this never touches
   the real sandbox.
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


def test_106_kes_airtime_for_100_kes_face_value():
    """Pins the exact example from the reseller agreement: 100 KES face
    value at a 6% discount yields 106 (106.38, truncated) KES of airtime."""
    face_value_kes = 100
    discount = 0.06

    airtime_value_kes = face_value_kes / (1 - discount)

    assert airtime_value_kes == pytest.approx(106.383, abs=0.001)
    assert int(airtime_value_kes) == 106


def test_procure_state_buys_airtime_at_6pct_discount(monkeypatch):
    """Drives the FSM's real PROCURE state for a 100 KES deposit and
    asserts the provider call it makes is for the discounted face value,
    not the raw 100 KES — this is the autonomous purchase step described
    in the reseller agreement (STK deposit -> immediate discounted buy)."""
    captured_calls = []

    def fake_post(url, **kwargs):
        captured_calls.append((url, kwargs.get("json")))
        if url.endswith("/api/auth/token"):
            return FakeResponse(200, {"success": True, "data": {"access_token": "token-123"}})
        if url.endswith("/send"):
            payload = kwargs.get("json", {})
            return FakeResponse(200, {
                "success": True,
                "data": {
                    "status": "success",
                    "requestRef": "REQ-TEST-1",
                    "walletBalance": 999,
                },
            })
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("services.impala_airtime.requests.post", fake_post)
    monkeypatch.setenv("AIRTIME_API_KEY", "test-key")
    monkeypatch.setenv("AIRTIME_API_SECRET", "test-secret")

    # Force a fresh client so the monkeypatched env vars above take effect
    # instead of whatever the module-level singleton already cached.
    from services.impala_airtime import ImpalaAirtimeClient
    monkeypatch.setattr(state_engine_module, "impala_airtime", ImpalaAirtimeClient())

    # N2 (Airtel) is the only node with a real procurement wallet wired —
    # point it at a harmless sandbox test number for this run.
    monkeypatch.setitem(state_engine_module.PROCUREMENT_WALLETS, "N2", "0733253036")

    base_rate = 129.50
    discount = 0.06
    face_value_kes = 100
    principal_usd = face_value_kes / base_rate  # what 100 KES of float is worth in USD

    ledger = ImmutableLedger(db_collection=None)
    fsm = HFTCorridorFSM(
        ledger,
        starting_capital_usd=principal_usd,
        config={
            "node_procure": "N2",
            "discount": discount,
            "baseline_rate": base_rate,
            "run_id": "TEST-RUN",
        },
    )

    asyncio.run(fsm._execute_procure())

    assert fsm.state == FSMState.LIQUIDATE, "PROCURE should succeed and hand off to LIQUIDATE"

    send_calls = [(url, body) for url, body in captured_calls if url.endswith("/send")]
    assert len(send_calls) == 1
    _, body = send_calls[0]
    assert body["amount"] == 106  # 100 KES face value / (1 - 0.06), truncated

    procure_entries = [e for e in ledger.local_records if e.txn_type.value == "PROCURE"]
    assert len(procure_entries) == 1
    assert procure_entries[0].amount == pytest.approx(106.383, abs=0.001)


def test_procure_state_halts_when_provider_rejects(monkeypatch):
    """If the provider rejects the discounted purchase, the FSM must halt
    rather than silently crediting airtime that was never actually bought."""
    def fake_post(url, **kwargs):
        if url.endswith("/api/auth/token"):
            return FakeResponse(200, {"success": True, "data": {"access_token": "token-123"}})
        if url.endswith("/send"):
            return FakeResponse(200, {"success": False, "message": "Insufficient provider float"})
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("services.impala_airtime.requests.post", fake_post)
    monkeypatch.setenv("AIRTIME_API_KEY", "test-key")
    monkeypatch.setenv("AIRTIME_API_SECRET", "test-secret")

    from services.impala_airtime import ImpalaAirtimeClient
    monkeypatch.setattr(state_engine_module, "impala_airtime", ImpalaAirtimeClient())
    monkeypatch.setitem(state_engine_module.PROCUREMENT_WALLETS, "N2", "0733253036")

    ledger = ImmutableLedger(db_collection=None)
    fsm = HFTCorridorFSM(
        ledger,
        starting_capital_usd=100 / 129.50,
        config={"node_procure": "N2", "discount": 0.06, "baseline_rate": 129.50, "run_id": "TEST-RUN-2"},
    )

    asyncio.run(fsm._execute_procure())

    assert fsm.state == FSMState.HALTED
    assert not any(e.txn_type.value == "PROCURE" for e in ledger.local_records)
