"""The withdrawal request to Comet: the live API wants amountBase as a JSON number, not the string the docs show."""
from services.comet_client import CometClient


def test_send_asset_sends_amount_base_as_a_number_not_a_string(monkeypatch):
    client = CometClient()
    seen = {}

    def fake_post(path, payload, use_service_token=False):
        seen["path"], seen["payload"] = path, payload
        return {"status": "success", "data": {"txHash": "0xT"}}

    monkeypatch.setattr(client, "_post", fake_post)
    client.send_asset("USDC", "254", "0xEXIT", client.to_base_units(0.030108))      # to_base_units returns a string
    assert seen["path"] == "/api/v1/assets/usdc/send"
    assert seen["payload"] == {"externalUserId": "254", "to": "0xEXIT", "amountBase": 30108}
    assert type(seen["payload"]["amountBase"]) is int                               # a JSON number on the wire
    assert isinstance(seen["payload"]["externalUserId"], str)                       # while the user id stays a string


def test_the_swap_amount_is_still_a_string(monkeypatch):
    """The swap endpoint accepted a string (the run's swap went through), so it must not be changed with the withdrawal."""
    client = CometClient()
    seen = {}
    monkeypatch.setattr(client, "_post", lambda path, payload, use_service_token=False: seen.update(payload=payload) or {"status": "success"})
    client.execute_amm_swap("254", "IMC", "USDC", "40541")
    assert seen["payload"]["amountIn"] == "40541"
