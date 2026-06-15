from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from database import get_db
from models.schemas import CardanoWithdrawRequest, CardanoVerifyDepositRequest
from auth import get_current_user
from config import settings

# router is defined immediately — cardano sub-module imports are deferred into
# each endpoint so a missing package never prevents this module from loading.
router = APIRouter(prefix="/api/cardano", tags=["cardano"])


def _cardano_guard():
    if not settings.blockfrost_project_id:
        raise HTTPException(
            status_code=503,
            detail="Cardano not configured: set BLOCKFROST_PROJECT_ID in .env",
        )
    if not settings.cardano_mnemonic:
        raise HTTPException(
            status_code=503,
            detail="Cardano not configured: set CARDANO_MNEMONIC in .env",
        )


def _import_cardano():
    """Lazy-import cardano sub-modules; raises 503 if packages are missing."""
    try:
        from cardano.wallet import CardanoWallet, get_or_create_wallet_index
        import cardano.usda as usda_ops
        return CardanoWallet, get_or_create_wallet_index, usda_ops
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Cardano packages not installed: {exc}. Run: pip install pycardano blockfrost-python",
        )


async def _wallet_for_user(db, current_user: dict):
    CardanoWallet, get_or_create_wallet_index, _ = _import_cardano()
    try:
        idx = await get_or_create_wallet_index(db, current_user["workspaceId"])
        return CardanoWallet(idx)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ── GET /api/cardano/wallet ───────────────────────────────────────────────────

@router.get("/wallet")
async def get_wallet(db=Depends(get_db), current_user=Depends(get_current_user)):
    CardanoWallet, get_or_create_wallet_index, usda_ops = _import_cardano()

    try:
        idx = await get_or_create_wallet_index(db, current_user["workspaceId"])
        wallet = CardanoWallet(idx)
        address = wallet.address_str
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    balance: dict = {"ada": None, "usda": None}
    if settings.blockfrost_project_id:
        try:
            balance = usda_ops.get_balance(address)
        except Exception:
            pass

    return {
        "address": address,
        "network": settings.cardano_network,
        "ada_balance": balance.get("ada"),
        "usda_balance": balance.get("usda"),
        "usda_policy_id": settings.usda_policy_id,
        "usda_asset_name_hex": settings.usda_asset_name_hex,
    }


# ── GET /api/cardano/balance ──────────────────────────────────────────────────

@router.get("/balance")
async def get_balance(db=Depends(get_db), current_user=Depends(get_current_user)):
    _cardano_guard()
    _, _, usda_ops = _import_cardano()
    wallet = await _wallet_for_user(db, current_user)
    try:
        return usda_ops.get_balance(wallet.address_str)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Blockfrost error: {exc}")


# ── GET /api/cardano/transactions ─────────────────────────────────────────────

@router.get("/transactions")
async def get_transactions(
    limit: int = 20,
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    _cardano_guard()
    _, _, usda_ops = _import_cardano()
    wallet = await _wallet_for_user(db, current_user)
    try:
        txs = usda_ops.get_usda_transactions(wallet.address_str, limit=min(limit, 50))
        return {"address": wallet.address_str, "transactions": txs}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Blockfrost error: {exc}")


# ── POST /api/cardano/on-ramp/verify ─────────────────────────────────────────

@router.post("/on-ramp/verify", status_code=201)
async def verify_on_ramp(
    body: CardanoVerifyDepositRequest,
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    _cardano_guard()
    _, _, usda_ops = _import_cardano()

    existing = await db.ramp_entries.find_one(
        {"cardanoTxHash": body.tx_hash, "workspaceId": current_user["workspaceId"]}
    )
    if existing:
        raise HTTPException(status_code=409, detail="Transaction already processed.")

    wallet = await _wallet_for_user(db, current_user)

    try:
        result = usda_ops.verify_deposit(body.tx_hash, wallet.address_str)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Blockfrost error: {exc}")

    usda_amount = result["usda_amount"]
    now = datetime.utcnow()

    ramp_doc = {
        "direction": "on",
        "channel": "Cardano",
        "fromAsset": "USDA",
        "toAsset": "USDA",
        "fromAmount": usda_amount,
        "toAmount": usda_amount,
        "rate": 1.0,
        "fee": 0.0,
        "counterparty": body.counterparty or "Cardano On-Chain",
        "status": "on",
        "cardanoTxHash": body.tx_hash,
        "cardanoAddress": wallet.address_str,
        "workspaceId": current_user["workspaceId"],
        "userId": current_user["_id"],
        "createdAt": now,
    }
    ramp_result = await db.ramp_entries.insert_one(ramp_doc)

    ledger_doc = {
        "date": now.strftime("%-d %b, %I:%M %p"),
        "flow": "On-Ramp",
        "description": f"Cardano On-Ramp: received {usda_amount:.6f} USDA — tx {body.tx_hash[:16]}…",
        "debitWallet": "USDA Wallet",
        "creditWallet": "Cardano",
        "debitAmount": usda_amount,
        "creditAmount": usda_amount,
        "debitAsset": "USDA",
        "creditAsset": "USDA",
        "counterparty": body.counterparty or "Cardano On-Chain",
        "workspaceId": current_user["workspaceId"],
        "userId": current_user["_id"],
        "createdAt": now,
    }
    await db.ledger_entries.insert_one(ledger_doc)

    return {
        "id": str(ramp_result.inserted_id),
        "tx_hash": body.tx_hash,
        "usda_received": usda_amount,
        "status": "confirmed",
        "message": f"On-ramp confirmed: {usda_amount} USDA credited to workspace.",
    }


# ── POST /api/cardano/withdraw ────────────────────────────────────────────────

@router.post("/withdraw", status_code=201)
async def withdraw_usda(
    body: CardanoWithdrawRequest,
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    _cardano_guard()
    _, _, usda_ops = _import_cardano()
    wallet = await _wallet_for_user(db, current_user)

    try:
        bal = usda_ops.get_balance(wallet.address_str)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Balance check failed: {exc}")

    if bal["usda"] < body.amount:
        raise HTTPException(
            status_code=422,
            detail=f"Insufficient USDA: have {bal['usda']}, need {body.amount}.",
        )

    min_ada = settings.cardano_min_utxo_lovelace / 1_000_000
    if (bal["ada"] or 0) < min_ada + 0.5:
        raise HTTPException(
            status_code=422,
            detail=f"Insufficient ADA for fees: need at least {min_ada + 0.5} ADA.",
        )

    try:
        tx_hash = usda_ops.send_usda(wallet, body.to_address, body.amount)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Transaction failed: {exc}")

    now = datetime.utcnow()

    ramp_doc = {
        "direction": "off",
        "channel": "Cardano",
        "fromAsset": "USDA",
        "toAsset": "USDA",
        "fromAmount": body.amount,
        "toAmount": body.amount,
        "rate": 1.0,
        "fee": 0.0,
        "counterparty": body.counterparty or body.to_address[:20] + "…",
        "status": "off",
        "cardanoTxHash": tx_hash,
        "cardanoAddress": body.to_address,
        "workspaceId": current_user["workspaceId"],
        "userId": current_user["_id"],
        "createdAt": now,
    }
    ramp_result = await db.ramp_entries.insert_one(ramp_doc)

    ledger_doc = {
        "date": now.strftime("%-d %b, %I:%M %p"),
        "flow": "Off-Ramp",
        "description": f"Cardano Off-Ramp: sent {body.amount:.6f} USDA — tx {tx_hash[:16]}…",
        "debitWallet": "Cardano",
        "creditWallet": "USDA Wallet",
        "debitAmount": body.amount,
        "creditAmount": body.amount,
        "debitAsset": "USDA",
        "creditAsset": "USDA",
        "counterparty": body.counterparty or body.to_address[:20] + "…",
        "workspaceId": current_user["workspaceId"],
        "userId": current_user["_id"],
        "createdAt": now,
    }
    await db.ledger_entries.insert_one(ledger_doc)

    return {
        "id": str(ramp_result.inserted_id),
        "tx_hash": tx_hash,
        "usda_sent": body.amount,
        "to_address": body.to_address,
        "status": "submitted",
        "message": f"Withdrawal submitted: {body.amount} USDA sent. Tx: {tx_hash}",
    }


# ── GET /api/cardano/tx/{tx_hash} ─────────────────────────────────────────────

@router.get("/tx/{tx_hash}")
async def get_transaction(
    tx_hash: str,
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    _cardano_guard()
    try:
        from cardano.client import get_blockfrost_api
        api = get_blockfrost_api()
        tx = api.transaction(tx_hash)
        utxos = api.transaction_utxos(tx_hash)
        return {
            "tx_hash": tx_hash,
            "block": tx.block,
            "block_height": tx.block_height,
            "fees": int(tx.fees) / 1_000_000,
            "inputs": [{"address": i.address, "amount": i.amount} for i in utxos.inputs],
            "outputs": [{"address": o.address, "amount": o.amount} for o in utxos.outputs],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Blockfrost error: {exc}")
