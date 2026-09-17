"""LIQUIDATE for internal market making: no STK push, no external buyer.

The corridor was originally built to STK-push-collect KES from a real
buyer's phone every cycle. That's wrong for "internal" market making — the
IMM already holds both the procured airtime and the paybill it operates,
so LIQUIDATE should recognize the airtime's discounted KES value directly
against money already sitting on the real paybill, gated by an actual
balance check (check_liquidity_threshold) so a cycle can never recognize
KES the paybill doesn't hold. This replaces the STK-push design entirely
(see git history for the version this superseded, and the "Invalid
input3" sandbox bug that version had)."""
import asyncio

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


def _fake_impala_send_ok(monkeypatch):
    """Fakes PROCURE's ImpalaPay call so _execute_liquidate is reachable
    without depending on the real sandbox's airtime float."""
    monkeypatch.setenv("AIRTIME_API_KEY", "test-key")
    monkeypatch.setenv("AIRTIME_API_SECRET", "test-secret")
    from services.impala_airtime import ImpalaAirtimeClient
    monkeypatch.setattr(state_engine_module, "impala_airtime", ImpalaAirtimeClient())
    monkeypatch.setitem(state_engine_module.PROCUREMENT_WALLETS, "N2", "0733253036")


def _impala_response(url):
    if url.endswith("/api/auth/token"):
        return FakeResponse(200, {"success": True, "data": {"access_token": "token-123"}})
    if url.endswith("/send"):
        return FakeResponse(200, {"success": True, "data": {"status": "success", "requestRef": "REQ-1"}})
    return None


def _build_fsm():
    ledger = ImmutableLedger(db_collection=None)
    return ledger, HFTCorridorFSM(
        ledger,
        starting_capital_usd=100 / 129.50,
        config={"node_procure": "N2", "node_liquidate": "N5", "discount": 0.06, "baseline_rate": 129.50, "run_id": "LIQ-TEST"},
    )


def _fake_daraja_auth_and_balance(monkeypatch, kes_balance):
    def fake_post(url, **kwargs):
        impala_resp = _impala_response(url)
        if impala_resp is not None:
            return impala_resp
        raise AssertionError(f"LIQUIDATE should never POST anything (no STK push): {url}")

    def fake_get(url, **kwargs):
        if url.endswith("/api/v1"):
            return FakeResponse(200, {"token": "jwt-123"})
        if url.endswith("/api/v1/wallet/balances"):
            return FakeResponse(200, {"balances": {"kesBalance": kes_balance}})
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("requests.get", fake_get)


def test_liquidate_recognizes_paybill_float_without_any_stk_push(monkeypatch):
    """The real fix: LIQUIDATE must succeed off a real balance check alone
    — no POST to /mobile/initiate, no STK push to anyone's phone."""
    _fake_impala_send_ok(monkeypatch)
    _fake_daraja_auth_and_balance(monkeypatch, kes_balance=600)

    ledger, fsm = _build_fsm()
    asyncio.run(fsm._execute_procure())
    assert fsm.state == FSMState.LIQUIDATE

    asyncio.run(fsm._execute_liquidate())

    assert fsm.state == FSMState.MINT
    liquidate_entries = [e for e in ledger.local_records if e.txn_type.value == "LIQUIDATE"]
    assert len(liquidate_entries) == 1
    assert liquidate_entries[0].external_ref is None  # no external provider reference — nothing external happened


def test_liquidate_halts_when_paybill_balance_is_insufficient(monkeypatch):
    """Refuses to recognize KES value the paybill doesn't actually hold —
    same fail-closed convention as check_min_balance/check_exposure_cap."""
    _fake_impala_send_ok(monkeypatch)
    _fake_daraja_auth_and_balance(monkeypatch, kes_balance=1)  # far below what this cycle needs

    ledger, fsm = _build_fsm()
    asyncio.run(fsm._execute_procure())
    assert fsm.state == FSMState.LIQUIDATE

    asyncio.run(fsm._execute_liquidate())

    assert fsm.state == FSMState.HALTED
    assert not any(e.txn_type.value == "LIQUIDATE" for e in ledger.local_records)
