"""Cardano native-asset operations for provider-backed AIRT."""
from __future__ import annotations

import hashlib
import os
from typing import Any

from pycardano import (
    Address,
    AlonzoMetadata,
    Asset,
    AssetName,
    AuxiliaryData,
    Metadata,
    MultiAsset,
    ScriptPubkey,
    TransactionBuilder,
    TransactionOutput,
    Value,
)

from cardano.client import get_chain_context
from cardano.wallet import CardanoWallet

AIRT_ASSET_NAME = os.getenv("CARDANO_AIRT_ASSET_NAME", "AIRT")
AIRT_DECIMALS = int(os.getenv("CARDANO_AIRT_DECIMALS", "0"))


def receipt_hash(provider: str, receipt_id: str, amount_kes: float, country: str) -> str:
    """Create a stable commitment without putting the provider receipt in public metadata."""
    payload = f"{provider.upper()}|{receipt_id.strip()}|{amount_kes:.6f}|{country.upper()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _policy_signing_key():
    raw_cbor = os.getenv("CARDANO_AIRT_POLICY_SIGNING_KEY", "").replace('"', '').replace("'", '').strip()
    if not raw_cbor:
        raise RuntimeError("CARDANO_AIRT_POLICY_SIGNING_KEY is not configured")
    from pycardano import PaymentExtendedSigningKey, PaymentSigningKey
    try:
        return PaymentSigningKey.from_cbor(raw_cbor)
    except Exception:
        return PaymentExtendedSigningKey.from_cbor(raw_cbor)


def _policy_script():
    policy_key = _policy_signing_key()
    from pycardano import PaymentVerificationKey
    script = ScriptPubkey(PaymentVerificationKey.from_signing_key(policy_key).hash())
    configured_policy_id = os.getenv("CARDANO_AIRT_POLICY_ID", "").strip().lower()
    derived_policy_id = str(script.hash()).lower()
    if configured_policy_id != derived_policy_id:
        raise RuntimeError(
            "CARDANO_AIRT_POLICY_ID does not match the policy signing key; refusing to mint"
        )
    return script, policy_key, derived_policy_id

# engine to mint airtime from cardano wallet to airtime provider
def mint_airt(
    funding_wallet: Any,
    recipient_address: str,
    token_amount: float,
    provider: str,
    provider_receipt_id: str,
    country: str,
    operation_id: str,
) -> dict[str, Any]:
    """Mint provider-backed AIRT and return the submitted Cardano transaction."""
    if token_amount <= 0 or token_amount != int(token_amount):
        raise ValueError("AIRT amount must be a positive whole number")
    if not provider_receipt_id.strip():
        raise ValueError("Provider receipt ID is required")

    policy, policy_key, policy_id = _policy_script()
    context = get_chain_context()
    recipient = Address.from_primitive(recipient_address)
    amount_units = int(token_amount) * (10 ** AIRT_DECIMALS)
    asset_name = AssetName(AIRT_ASSET_NAME.encode("ascii"))

    minted_assets = MultiAsset()
    minted_assets[policy.hash()] = Asset({asset_name: amount_units})
    output_value = Value(
        coin=int(os.getenv("CARDANO_AIRT_MIN_UTXO_LOVELACE", "2000000")),
        multi_asset=minted_assets,
    )

    commitment = receipt_hash(provider, provider_receipt_id, token_amount, country)
    metadata = Metadata({
        674: {
            "asset": AIRT_ASSET_NAME,
            "operation_id": operation_id,
            "provider": provider.upper(),
            "receipt_hash": commitment,
            "reserve_value_kes": int(token_amount),
        }
    })
    builder = TransactionBuilder(
        context,
        native_scripts=[policy],
        mint=minted_assets,
        auxiliary_data=AuxiliaryData(AlonzoMetadata(metadata=metadata, native_scripts=[policy])),
    )
    builder.add_input_address(funding_wallet.address)
    builder.add_output(TransactionOutput(recipient, output_value))
    signed_tx = builder.build_and_sign(
        signing_keys=[funding_wallet.signing_key, policy_key],
        change_address=funding_wallet.address,
    )
    context.submit_tx(signed_tx.to_cbor())
    return {
        "tx_hash": str(signed_tx.id),
        "policy_id": policy_id,
        "asset_name": AIRT_ASSET_NAME,
        "token_amount": int(token_amount),
        "receipt_hash": commitment,
    }



# engine  to burn airtime 
def burn_airt(
    funding_wallet: Any,
    token_amount: float,
    operation_id: str,
) -> dict[str, Any]:
    """Burn AIRT held by the custodial wallet and return the submitted transaction."""
    if token_amount <= 0 or token_amount != int(token_amount):
        raise ValueError("AIRT amount must be a positive whole number")

    policy, policy_key, policy_id = _policy_script()
    context = get_chain_context()
    asset_name = AssetName(AIRT_ASSET_NAME.encode("ascii"))
    burned_assets = MultiAsset()
    burned_assets[policy.hash()] = Asset({asset_name: -int(token_amount) * (10 ** AIRT_DECIMALS)})
    metadata = Metadata({674: {
        "asset": AIRT_ASSET_NAME,
        "operation_id": operation_id,
        "action": "BURN",
        "amount": int(token_amount),
    }})
    builder = TransactionBuilder(
        context,
        native_scripts=[policy],
        mint=burned_assets,
        auxiliary_data=AuxiliaryData(AlonzoMetadata(metadata=metadata, native_scripts=[policy])),
    )
    builder.add_input_address(funding_wallet.address)
    signed_tx = builder.build_and_sign(
        signing_keys=[funding_wallet.signing_key, policy_key],
        change_address=funding_wallet.address,
    )
    context.submit_tx(signed_tx.to_cbor())
    return {
        "tx_hash": str(signed_tx.id),
        "policy_id": policy_id,
        "asset_name": AIRT_ASSET_NAME,
        "token_amount": int(token_amount),
    }
