import pytest
from pydantic import BaseModel

from routes.ramp import resolve_crypto_destination_address
from routes.swap_engine import validate_swap_request


class SwapRequest(BaseModel):
    user_id: str
    from_asset: str
    to_asset: str
    from_amount: float
    destination_address: str | None = None
    destination_phone: str | None = None


def test_validate_swap_request_uses_request_values_for_fiat_routes():
    req = SwapRequest(
        user_id="user-123",
        from_asset="KES",
        to_asset="UGX",
        from_amount=1000,
        destination_phone="+256700000000",
    )

    # This should validate successfully without raising.
    validate_swap_request(req)


def test_validate_swap_request_requires_phone_for_mobile_money():
    req = SwapRequest(
        user_id="user-123",
        from_asset="KES",
        to_asset="UGX",
        from_amount=1000,
        destination_phone="",
    )

    with pytest.raises(ValueError):
        validate_swap_request(req)


def test_resolve_crypto_destination_address_prefers_valid_celo_wallet():
    user = {"walletAddress": "0x1234567890123456789012345678901234567890"}
    result = resolve_crypto_destination_address("0xabcdefabcdefabcdefabcdefabcdefabcdefabcd", user)
    assert result == "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"

    result = resolve_crypto_destination_address("", user)
    assert result == user["walletAddress"]

    result = resolve_crypto_destination_address("", {"walletAddress": "addr1v84ygw3we0sdyxt6208q08rg4gqql9209y9w328kkmsr5sqrwtrtv"})
    assert result == "0x6f7BeAb48EAfC47B89041899a35a0525a6A60F59"

    result = resolve_crypto_destination_address("", {"walletAddress": "not-a-wallet"})
    assert result == "0x6f7BeAb48EAfC47B89041899a35a0525a6A60F59"
