from __future__ import annotations

from datetime import datetime
from typing import Optional, Dict
from bson import ObjectId
from fastapi import APIRouter, Depends, Request, HTTPException, status 
from pydantic import BaseModel, Field
from fastapi import Body
import os

from database import get_db
from auth import get_current_user
from config import settings
from models.schemas import CardanoWithdrawRequest, CardanoVerifyDepositRequest

from pycardano import (
    TransactionBuilder,
    TransactionOutput,
    MultiAsset,
    AssetName,
    Value,
    ScriptPubkey,
    BlockFrostChainContext,
    PaymentKeyPair,
    PaymentSigningKey,
    Network,
    Address

    
)
from blockfrost import ApiUrls, BlockFrostApi

# Import the security utility 
from security.encryption import decrypt_private_key

# Initialize the router ONCE at the top
router = APIRouter(prefix="/api/cardano", tags=["cardano"])

def get_master_signing_key() -> PaymentSigningKey:
    """
    Unlocks the company's Master Wallet Private Key from the .env vault.
    Requires MAMLAKA_MASTER_PASSWORD to be set in the server environment.
    """
    encrypted_key = os.getenv("ENCRYPTED_MASTER_KEY")
    salt = os.getenv("WALLET_SALT")
    password = os.getenv("MAMLAKA_MASTER_PASSWORD") # This is imported on the servers terminal to inhect the password from here which is invisible servers RAM.
                                                    # export MAMLAKA_MASTER_PASSWORD="password"

    if not encrypted_key or not salt:
        raise HTTPException(status_code=500, detail= "Server Error: Encrypted key or salt missing from .env")
    
    if not password:
        raise HTTPException(status_code=500, detail="SERVER SECURE HALT: Master Password not provided. Cannot unlock treasury.")

    try:
        # Decrypt the cborHex string 
        raw_cbor = decrypt_private_key(encrypted_key, salt, password)

        # Convert it back into pycardano signing key
        return PaymentSigningKey.from_cbor(raw_cbor)
    except Exception as e:
           raise HTTPException(status_code=403, detail="CRITICAL: Failed to unlock Master Wallet. Incorrect password?")


#================================================
# Checks  for New encrypted vault with set password from the server 
#================================================
def _cardano_guard():
    if not os.getenv("BLOCKFROST_PROJECT_ID"):
        raise HTTPException(
            status_code=503,
            detail="Cardano not configured: set BLOCKFROST_PROJECT_ID in .env",
        )
    
    # NEW SECURITY GUARD: Check for the encrypted vault instead of the mnemonic
    if not os.getenv("ENCRYPTED_MASTER_KEY") or not os.getenv("WALLET_SALT"):
        raise HTTPException(
            status_code=503,
            detail="Vault Error: Missing ENCRYPTED_MASTER_KEY or WALLET_SALT in .env",
        )
        
    # Check if the terminal has the password injected in its memory
    if not os.getenv("MAMLAKA_MASTER_PASSWORD"):
        raise HTTPException(
            status_code=503,
            detail="Security Halt: Master Password not injected! Run 'export MAMLAKA_MASTER_PASSWORD=\"yourpassword\"' in your terminal.",
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
async def get_deposit_wallet(current_user=Depends(get_current_user)):
    """
    Custodial Model: Returns the Company's Master Wallet address to user dashboard
    for users to deposit their funds into.
    """
    import os
    from dotenv import load_dotenv
    
    # Force reload .env to ensure we get the latest Mainnet address
    load_dotenv(override=True)
    
    master_address = os.getenv("MASTER_WALLET_ADDRESS")
    
    if not master_address:
        raise HTTPException(status_code=500, detail="Master Vault address not configured on server.")
        
    return {
        "address": master_address,
        # Mock estimated fees for the UI
        "estimated_fee_ada": 0.17,
        "estimated_fee_usd": 0.06,
        "message": "Deposit to Master Vault"
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


class FeeEstimateRequest(BaseModel):
        to_address: str
        amount: float
        asset: str = "USDA"


@router.post("/estimate-fee")
async def estimate_fee(body: FeeEstimateRequest = Body(...), db=Depends(get_db), current_user=Depends(get_current_user)):
        _cardano_guard()
        CardanoWallet, get_or_create_wallet_index, usda_ops = _import_cardano()

        wallet = await _wallet_for_user(db, current_user)

        if body.asset.upper() != "USDA":
            raise HTTPException(status_code=400, detail="Only USDA fee estimation is supported currently.")

        try:
            fee_lovelace = usda_ops.estimate_usda_fee(wallet, body.to_address, body.amount)
            fee_ada = fee_lovelace / 1_000_000
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except Exception as exc:
            fee_ada = settings.cardano_min_utxo_lovelace / 1_000_000

        fee_usd = None
        if getattr(settings, "cardano_ada_usd_rate", None):
            fee_usd = round(fee_ada * settings.cardano_ada_usd_rate, 6)

        return {"estimated_fee_ada": fee_ada, "estimated_fee_usd": fee_usd}


class TopUpRequest(BaseModel):
        to_address: str
        amount: float
        asset: str = "USDA"


@router.post("/topup", status_code=201)
async def platform_topup(body: TopUpRequest, db=Depends(get_db), current_user=Depends(get_current_user)):
        users = db.users
        user_doc = await users.find_one({"_id": ObjectId(current_user["_id"])})
        if not user_doc or user_doc.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin role required for platform top-up.")

        _cardano_guard()
        CardanoWallet, _, usda_ops = _import_cardano()

        platform_idx = getattr(settings, "cardano_platform_account_index", 0)
        try:
            platform_wallet = CardanoWallet(platform_idx)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))

        if body.asset.upper() != "USDA":
            raise HTTPException(status_code=400, detail="Only USDA top-up is supported via this endpoint.")

        try:
            tx_hash = usda_ops.send_usda(platform_wallet, body.to_address, body.amount)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Top-up transaction failed: {exc}")

        return {"ok": True, "tx_hash": tx_hash}


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

# ── POST /api/cardano/mint-real-usda ──────────────────────────────────────────

@router.post("/mint-real-usda", status_code=201)
async def mint_real_usda():
    _cardano_guard()
    CardanoWallet, _, usda_ops = _import_cardano()
    
    platform_wallet = CardanoWallet(0)
    print(f"\n MY MASTER WALLET ADRESS IS: {platform_wallet.address_str}\n")
    
    project_id = os.getenv("BLOCKFROST_PROJECT_ID") or getattr(settings, "blockfrost_project_id", "")
    
    context = BlockFrostChainContext(
        project_id = project_id,
        base_url= ApiUrls.preprod.value
    )

    signing_key = platform_wallet.signing_key
    verification_key = signing_key.to_verification_key()
    
    policy_script = ScriptPubkey(verification_key.hash())
    policy_id = policy_script.hash()
    
    asset_name = AssetName(b"USDA")
    raw_amount_to_mint = 1_000_000_000_000
    
    builder = TransactionBuilder(context)
    builder.add_input_address(platform_wallet.address)
    
    builder.mint = MultiAsset.from_primitive({
        policy_id.payload: {asset_name.payload: raw_amount_to_mint}
    })
    builder.native_scripts = [policy_script]
    
    builder.add_output(TransactionOutput(
        platform_wallet.address,
        Value(
            coin=2000000, 
            multi_asset=builder.mint
        )
    ))

    signed_tx = builder.build_and_sign(
        [signing_key], 
        change_address=platform_wallet.address
    )
    
    context.submit_tx(signed_tx.to_cbor())
    
    return {
        "message": "Successfully printed 1,000,000 real Testnet USDA!",
        "new_policy_id": str(policy_id),
        "tx_hash": str(signed_tx.id),
        "platform_address": str(platform_wallet.address)
    }

# USDA Policy ID is the unique fingerprint of your stablecoin on-chain

@router.get("/master-wallet/balance")
async def get_master_balance():
    import os
    from datetime import datetime
    from blockfrost import ApiUrls, BlockFrostApi
    from dotenv import load_dotenv
    
    # 1. Force reload .env so you don't have to restart the server
    load_dotenv(override=True)
    
    # 2. Dynamically select Mainnet vs Preprod based on .env
    current_network = os.getenv("CARDANO_NETWORK", "testnet").lower()
    api_url = ApiUrls.mainnet.value if current_network == "mainnet" else ApiUrls.preprod.value
    
    api = BlockFrostApi(
        project_id=os.getenv("BLOCKFROST_PROJECT_ID"),
        base_url=api_url
    )
     
    master_address = os.getenv("MASTER_WALLET_ADDRESS")
    usda_policy_id = os.getenv("USDA_POLICY_ID", "").lower()
    usda_hex = "55534441" # 'USDA' encoded in hexadecimal on Cardano
    
    try:
        address_info = api.address(address=master_address)
        
        # Look for the USDA asset specifically
        usda_balance = 0
        for asset in address_info.amount:
            unit_lower = asset.unit.lower()
            
            # Match either the Policy ID OR the exact token name 'USDA' in hex
            if (usda_policy_id and unit_lower.startswith(usda_policy_id)) or unit_lower.endswith(usda_hex.lower()):
                # asset.quantity is returned in raw Lovelace/Smallest unit
                usda_balance = int(asset.quantity) / 1_000_000
                break
                
        return {
            "balance": usda_balance,
            "unit": "USDA",
            "lastUpdated": datetime.utcnow().isoformat(),
            "network": current_network
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch balance: {str(e)}")
# ── GET /api/cardano/master/adress ─────────────────────────────

@router.get("/master/address")
async def get_master_address():
    """
    Generates a fresh Cardano wallet.
    Dynamically checks .env to generate Mainnet (addr1) or Testnet (addr_test1) addresses.
    """
    # 1. generate a new cryptographic key pair
    key_pair = PaymentKeyPair.generate()

    # 2. Check the .env network setting
    current_network = getattr(settings, "cardano_network", "testnet").lower()
    is_mainnet = (current_network == "mainnet")
    
    network_enum = Network.MAINNET if is_mainnet else Network.TESTNET
    network_label = "Mainnet" if is_mainnet else "Preprod Testnet"

    # 3. derive the address from public verification key
    new_address = Address(payment_part=key_pair.verification_key.hash(), network=network_enum)

    return {
        "address" : str(new_address),
        "private_key": str(key_pair.signing_key),
        "network": network_label,
        "message": "SECURITY WARNING: This key controls REAL money. Store securely!" if is_mainnet else "We use this address as your counterparty for testing"
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

    # Status set to 'COMPLETED' here because the transaction 
    # was already executed successfully above.
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
        "status": "COMPLETED", 
        "cardanoTxHash": tx_hash,
        "cardanoAddress": body.to_address,
        "workspaceId": current_user["workspaceId"],
        "userId": current_user["_id"],
        "createdAt": now,
    }
    ramp_result = await db.ramp_entries.insert_one(ramp_doc)

    return {
        "id": str(ramp_result.inserted_id),
        "tx_hash": tx_hash,
        "amount_sent": body.amount,
        "status": "COMPLETED",
        "message": f"Withdrawal of {body.amount} USDA has been successfully processed.",
    }

# ── POST /api/cardano/off-ramp ───────────────────────────────────────────────

class OffRampRequest(BaseModel):
    amount: float = Field(..., gt=0, description="The amount of USDA to withdraw")
    phone_number: str = Field(..., description="The M-Pesa phone number to receive KES (e.g., 254712345678)")

@router.post("/off-ramp", status_code=status.HTTP_202_ACCEPTED)
async def execute_off_ramp(
    body: OffRampRequest, 
    db=Depends(get_db),
    current_user: Dict = Depends(get_current_user)
):
    """
    Executes a secure Web3 to Web2 Off-Ramp.
    Sweeps USDA from the Hot Wallet back to the Master Wallet for Fiat payout.
    """
    _cardano_guard()
    _, _, usda_ops = _import_cardano()
    
    try:
        # 1. Get the Master Wallet Address from .env
        master_address = os.getenv("MASTER_WALLET_ADDRESS")
        if not master_address:
            raise HTTPException(status_code=500, detail="Platform configuration error: Master address missing.")
        
        # 2. Derive the specific Hot Wallet for the logged-in user
        user_hot_wallet = await _wallet_for_user(db, current_user)
        
        # 3. Build and Sign the Transaction
        print(f"Sweeping {body.amount} USDA from Hot Wallet {user_hot_wallet.address_str} to Master Vault {master_address}...")
        
        # Use the existing send_usda function to sweep the funds back
        tx_hash = usda_ops.send_usda(
            from_wallet=user_hot_wallet, 
            to_address=master_address, 
            amount=body.amount
        )
        
        # 4. Record the pending fiat payout in MongoDB
        now = datetime.utcnow()
        withdrawal_record = {
            "direction": "off",
            "channel": "M-Pesa",
            "fromAsset": "USDA",
            "toAsset": "KES",
            "fromAmount": body.amount,
            "toAmount": body.amount * 130.0, # Calculate actual KES based on your exchange rate
            "rate": 130.0,
            "fee": 0.0,
            "counterparty": body.phone_number,
            "status": "pending_fiat_payout",
            "cardanoTxHash": tx_hash,
            "cardanoAddress": master_address, # Funds sent here
            "workspaceId": current_user["workspaceId"],
            "userId": current_user["_id"],
            "createdAt": now,
        }
        await db.ramp_entries.insert_one(withdrawal_record)
        
        return {
            "success": True,
            "message": "Crypto sweep initiated successfully. Processing fiat payout next.",
            "tx_hash": tx_hash
        }

    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, 
            detail=f"Off-ramp blockchain transaction failed: {exc}"
        )

# ── POST /api/cardano/webhook ─────────────────────────────────────────────────

@router.post("/webhook")
async def provider_webhook(request: Request, db=Depends(get_db)):
    """Receive provider callbacks for deposit confirmations."""
    body = await request.json()
    tx_hash = body.get("txHash") or body.get("tx_hash")
    confirmations = int(body.get("confirmations", 0))
    if not tx_hash:
        raise HTTPException(status_code=400, detail="txHash required")

    await db.ramp_entries.update_one(
        {"cardanoTxHash": tx_hash}, 
        {"$set": {"confirmations": confirmations, "status": "on" if confirmations >= 10 else "pending"}}
    )
    return {"ok": True}