import types

import pytest

from services.impala_airtime import ImpalaAirtimeClient


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


def test_send_airtime_raises_cleanly_on_404_without_a_mamlaka_fallback(monkeypatch):
    """ImpalaPay's real Reseller API (airtime-api.impalapay.com) is the
    only real airtime source this platform buys from — Mamlaka/Lipad is a
    separate M-Pesa payment gateway (see services/safaricom_daraja.py) and
    must never silently stand in for a failed ImpalaPay call. A 404 on
    POST /api/app/airtime should fail loudly, not quietly reroute the
    purchase to a different provider. Uses a pre-provisioned API key
    (IMPALA_RESELLER_API_KEY) so send_airtime never needs the
    login/session-token round trip — only the airtime call itself."""
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/api/app/airtime"):
            return FakeResponse(404, {}, "<html>Cannot POST /api/app/airtime</html>")
        raise AssertionError(f"Unexpected URL: {url} — no fallback call should ever be made")

    monkeypatch.setattr("services.impala_airtime.requests.post", fake_post)
    monkeypatch.setenv("IMPALA_RESELLER_API_KEY", "test-api-key")
    client = ImpalaAirtimeClient()

    with pytest.raises(RuntimeError, match="ImpalaPay airtime send failed"):
        client.send_airtime("0712345678", 100, "REF-123")

    assert not any(url.endswith("/api/v1/mobile/airtime") for url, _ in calls)


def test_send_airtime_rejects_business_failure_even_when_http_200(monkeypatch):
    """Real response shape: {"error": bool, "message": str, ...} — success
    is error == False, not a "success" field. A 200 with error: true must
    still raise, not be treated as a successful send."""
    def fake_post(url, **kwargs):
        if url.endswith("/api/app/airtime"):
            return FakeResponse(200, {"error": True, "message": "Unsupported airtime provider"})
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("services.impala_airtime.requests.post", fake_post)
    monkeypatch.setenv("IMPALA_RESELLER_API_KEY", "test-api-key")
    client = ImpalaAirtimeClient()

    with pytest.raises(RuntimeError, match="Provider rejected the airtime request"):
        client.send_airtime("0712345678", 100, "REF-123")


def test_provider_name_for_phone_detects_airtel_numbers_with_leading_zero():
    client = ImpalaAirtimeClient()
    assert client._provider_name_for_phone("0733253036") == "Airtel"
    assert client._provider_name_for_phone("0743253036") == "Airtel"
    assert client._provider_name_for_phone("0753253036") == "Airtel"
    assert client._provider_name_for_phone("0783253036") == "Airtel"
    assert client._provider_name_for_phone("254783253036") == "Airtel"
