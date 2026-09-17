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
    """ImpalaPay is the only real airtime source this platform buys from —
    Mamlaka/Lipad is a separate M-Pesa payment gateway (see
    services/safaricom_daraja.py) and must never silently stand in for a
    failed ImpalaPay call. A 404 on the send route should fail loudly, not
    quietly reroute the purchase to a different provider."""
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/api/auth/token"):
            return FakeResponse(200, {"success": True, "data": {"access_token": "token-123"}})
        if url.endswith("/send"):
            return FakeResponse(404, {}, "<html>Cannot POST /send</html>")
        raise AssertionError(f"Unexpected URL: {url} — no fallback call should ever be made")

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(200, {"token": "jwt-123"})

    monkeypatch.setattr("services.impala_airtime.requests.post", fake_post)
    monkeypatch.setattr("services.impala_airtime.requests.get", fake_get)
    monkeypatch.setenv("LIPAD_BASE_URL", "https://payments.mam-laka.com")
    monkeypatch.setenv("LIPAD_API_USERNAME", "meshex_sandbox")
    monkeypatch.setenv("LIPAD_API_PASSWORD", "secret")
    client = ImpalaAirtimeClient()

    with pytest.raises(RuntimeError, match="ImpalaPay airtime send failed"):
        client.send_airtime("0712345678", 100, "REF-123")

    assert not any(url.endswith("/api/v1/mobile/airtime") for url, _ in calls)


def test_send_airtime_rejects_business_failure_even_when_http_200(monkeypatch):
    def fake_post(url, **kwargs):
        if url.endswith("/api/auth/token"):
            return FakeResponse(200, {"success": True, "data": {"access_token": "token-123"}})
        if url.endswith("/send"):
            return FakeResponse(200, {"success": False, "message": "Unsupported airtime provider"})
        if url.endswith("/api/v1/mobile/airtime"):
            return FakeResponse(200, {"status": "failed", "message": "Airtime disbursion failed", "transactionId": "TEST-REF-001"})
        raise AssertionError(f"Unexpected URL: {url}")

    def fake_get(url, **kwargs):
        return FakeResponse(200, {"token": "jwt-123"})

    monkeypatch.setattr("services.impala_airtime.requests.post", fake_post)
    monkeypatch.setattr("services.impala_airtime.requests.get", fake_get)
    monkeypatch.setenv("LIPAD_BASE_URL", "https://payments.mam-laka.com")
    monkeypatch.setenv("LIPAD_API_USERNAME", "meshex_sandbox")
    monkeypatch.setenv("LIPAD_API_PASSWORD", "secret")
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
