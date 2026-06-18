"""
USDA-specific Cardano operations:
  - query balance
  - query transaction history
  - verify an inbound deposit tx
  - build + sign + submit a withdrawal
"""
from __future__ import annotations

from datetime import datetime

from config import settings
from cardano.client import get_blockfrost_api, get_chain_context
from cardano.wallet import CardanoWallet


# ── helpers ──────────────────────────────────────────────────────────────────

def _usda_asset_id() -> str:
    return settings.usda_policy_id + settings.usda_asset_name_hex


def _to_usda(raw: int) -> float:
    return raw / 10 ** settings.usda_decimals


def _from_usda(amount: float) -> int:
    return int(amount * 10 ** settings.usda_decimals)


# ── balance ──────────────────────────────────────────────────────────────────

def get_balance(address: str) -> dict:
    """Return ADA (in ADA) and USDA balance for an address."""
    api = get_blockfrost_api()
    asset_id = _usda_asset_id()

    try:
        addr_info = api.address(address)
        ada_lovelace = int(addr_info.amount[0].quantity) if addr_info.amount else 0
        usda_raw = 0
        for a in addr_info.amount:
            if a.unit == asset_id:
                usda_raw = int(a.quantity)
                break
    except Exception:
        ada_lovelace = 0
        usda_raw = 0

    return {
        "address": address,
        "ada": round(ada_lovelace / 1_000_000, 6),
        "usda": _to_usda(usda_raw),
        "usda_raw": usda_raw,
    }


# ── transaction history ───────────────────────────────────────────────────────

def get_usda_transactions(address: str, limit: int = 20) -> list[dict]:
    """Return recent USDA-related transactions for an address."""
    api = get_blockfrost_api()
    asset_id = _usda_asset_id()

    try:
        txs = api.address_transactions(address, count=limit, order="desc")
    except Exception:
        return []

    results = []
    for tx_ref in txs:
        try:
            tx = api.transaction_utxos(tx_ref.tx_hash)
            usda_in = 0
            usda_out = 0

            for inp in tx.inputs:
                if inp.address == address:
                    for amt in inp.amount:
                        if amt.unit == asset_id:
                            usda_in += int(amt.quantity)

            for out in tx.outputs:
                if out.address == address:
                    for amt in out.amount:
                        if amt.unit == asset_id:
                            usda_out += int(amt.quantity)

            net_raw = usda_out - usda_in
            if net_raw == 0:
                continue

            direction = "receive" if net_raw > 0 else "send"
            results.append({
                "tx_hash": tx_ref.tx_hash,
                "direction": direction,
                "usda_amount": _to_usda(abs(net_raw)),
                "block_time": datetime.utcfromtimestamp(tx_ref.block_time).isoformat()
                if hasattr(tx_ref, "block_time") and tx_ref.block_time
                else None,
                "block_height": tx_ref.block_height,
            })
        except Exception:
            continue

    return results


# ── on-ramp verification ──────────────────────────────────────────────────────

def verify_deposit(tx_hash: str, expected_address: str) -> dict:
    """
    Verify that a given tx hash delivered USDA to expected_address.
    Returns the USDA amount received or raises ValueError.
    """
    api = get_blockfrost_api()
    asset_id = _usda_asset_id()

    tx_utxos = api.transaction_utxos(tx_hash)
    usda_received = 0
    for out in tx_utxos.outputs:
        if out.address == expected_address:
            for amt in out.amount:
                if amt.unit == asset_id:
                    usda_received += int(amt.quantity)

    if usda_received == 0:
        raise ValueError(
            f"Transaction {tx_hash} delivered no USDA to address {expected_address}."
        )

    return {
        "tx_hash": tx_hash,
        "usda_amount": _to_usda(usda_received),
        "usda_raw": usda_received,
    }


# ── withdrawal (off-ramp) ─────────────────────────────────────────────────────

def send_usda(wallet: CardanoWallet, to_address_str: str, amount: float) -> str:
    """
    Build, sign, and submit a USDA transfer from the workspace hot wallet.
    Returns the transaction hash.
    """
    try:
        from pycardano import (
            TransactionBuilder,
            TransactionOutput,
            Address,
            Value,
            MultiAsset,
            Asset,
            AssetName,
            ScriptHash,
        )
    except ImportError:
        raise RuntimeError("pycardano is not installed. Run: pip install pycardano")

    context = get_chain_context()
    to_address = Address.from_primitive(to_address_str)
    usda_amount = _from_usda(amount)

    policy_bytes = bytes.fromhex(settings.usda_policy_id)
    asset_name_bytes = bytes.fromhex(settings.usda_asset_name_hex)

    script_hash = ScriptHash(policy_bytes)
    asset_name = AssetName(asset_name_bytes)

    multi_asset = MultiAsset()
    multi_asset[script_hash] = Asset()
    multi_asset[script_hash][asset_name] = usda_amount

    value = Value(
        coin=settings.cardano_min_utxo_lovelace,
        multi_asset=multi_asset,
    )

    builder = TransactionBuilder(context)
    builder.add_input_address(wallet.address)
    builder.add_output(TransactionOutput(to_address, value))

    signed_tx = builder.build_and_sign(
        signing_keys=[wallet.signing_key],
        change_address=wallet.address,
    )

    context.submit_tx(signed_tx.to_cbor())
    return str(signed_tx.id)


def estimate_usda_fee(wallet: CardanoWallet, to_address_str: str, amount: float) -> int:
    """
    Build and sign a USDA transaction but do not submit it; return estimated fee in lovelace.
    Falls back to a conservative estimate if precise fee isn't extractable.
    """
    try:
        from pycardano import (
            TransactionBuilder,
            TransactionOutput,
            Address,
            Value,
            MultiAsset,
            Asset,
            AssetName,
            ScriptHash,
        )
    except ImportError:
        raise RuntimeError("pycardano is not installed. Run: pip install pycardano")

    context = get_chain_context()
    to_address = Address.from_primitive(to_address_str)
    usda_amount = _from_usda(amount)

    policy_bytes = bytes.fromhex(settings.usda_policy_id)
    asset_name_bytes = bytes.fromhex(settings.usda_asset_name_hex)

    script_hash = ScriptHash(policy_bytes)
    asset_name = AssetName(asset_name_bytes)

    multi_asset = MultiAsset()
    multi_asset[script_hash] = Asset()
    multi_asset[script_hash][asset_name] = usda_amount

    value = Value(
        coin=settings.cardano_min_utxo_lovelace,
        multi_asset=multi_asset,
    )

    builder = TransactionBuilder(context)
    builder.add_input_address(wallet.address)
    builder.add_output(TransactionOutput(to_address, value))

    # Build and sign locally to obtain fee, but do not submit
    signed_tx = builder.build_and_sign(
        signing_keys=[wallet.signing_key],
        change_address=wallet.address,
    )

    # Attempt to extract fee
    fee_lovelace = None
    try:
        # pycardano SignedTransaction may expose body.fee
        fee_lovelace = int(getattr(signed_tx, "fee", None) or getattr(getattr(signed_tx, "body", None), "fee", None))
    except Exception:
        fee_lovelace = None

    # Fallback: compute fee as sum(inputs) - sum(outputs) if available
    if fee_lovelace is None:
        try:
            inputs_total = 0
            outputs_total = 0
            for inp in signed_tx.body.inputs:
                inputs_total += sum([int(a.quantity) for a in inp.value.multi_asset.get_raw_amounts()] ) if hasattr(inp.value, 'multi_asset') else int(inp.value.coin)
            for out in signed_tx.body.outputs:
                outputs_total += int(out.amount.coin) if hasattr(out, 'amount') else int(out.value.coin)
            fee_lovelace = max(0, inputs_total - outputs_total)
        except Exception:
            fee_lovelace = settings.cardano_min_utxo_lovelace

    return int(fee_lovelace)
