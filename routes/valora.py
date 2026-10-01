import os
import uuid
import asyncio
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from database import get_db
from routes.auth import get_current_user, get_verified_current_user
from celo_wallet import derive_celo_account, get_or_create_celo_wallet_index
from celo_audit import log_celo_audit_event
from flutterwave_payouts import list_banks, settle_bank_transfer
from fiat_payout_audit import log_fiat_payout_audit_event
from notifications import notify_user
from two_factor import verify_withdrawal_2fa
from wallet_utils import debit_wallet, credit_wallet
from config import settings
from dotenv import load_dotenv

# 🟢 NEW: Safely convert String IDs to MongoDB ObjectIds
try:
    from bson import ObjectId
except ImportError:
    ObjectId = None

def safe_object_id(val):
    if ObjectId and isinstance(val, str) and len(val) == 24:
        try:
            return ObjectId(val)
        except:
            pass
    return val


def build_user_id_candidates(val):
    """Return all likely userId representations (string/ObjectId)."""
    candidates = []

    if val is None:
        return candidates

    candidates.append(val)
    val_str = str(val)
    if val_str not in candidates:
        candidates.append(val_str)

    oid = safe_object_id(val_str)
    if oid not in candidates:
        candidates.append(oid)

    return candidates

load_dotenv(override=True)

router = APIRouter(prefix="/api/valora", tags=["Celo Wallets"])

# --- 1. WEB3 & CELO CONFIGURATION ---
CELO_RPC = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
CHAIN_ID = 42220

w3 = Web3(Web3.HTTPProvider(CELO_RPC, request_kwargs={'timeout': 15}))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

ASSET_CONTRACTS = {
    "cUSD": w3.to_checksum_address("0x765DE816845861e75A25fCA122bb6898B8B1282a"),
    "USDC": w3.to_checksum_address("0xcebA9300f2b948710d2653dD7B07f33A8B32118C"),
    "USDT": w3.to_checksum_address("0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e"),
    # Impala Coin (IMC) — real, deployed Celo mainnet contract. Confirmed
    # 2026-09-25 via a live balanceOf/symbol/decimals read: name "Impala
    # Coin", symbol "IMC", 6 decimals (matches Comet's tokenize_airtime
    # amountBase convention exactly). totalSupply was 0 at confirmation
    # time — nothing has been minted on this contract yet, so a balance
    # read against it will legitimately return 0 until the first real
    # tokenize_airtime call succeeds.
    "IMC": w3.to_checksum_address("0x766AA4F469A295330b10D150f842d71977D56dD6"),
}

# "USD" isn't its own on-chain token — there's no US banking rail behind this
# platform, so a "USD" balance is a ledger label over a real, fully-backed
# USDC settlement on Celo. Every on-chain operation for a "USD" withdrawal
# uses USDC's contract/decimals; only the internal retail_wallets field (and
# the amount debited/credited there) stays "USD". Added 2026-09-04.
ONCHAIN_SETTLEMENT_ALIAS = {"USD": "USDC"}

ERC20_ABI = [
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"anonymous": False, "inputs": [{"indexed": True, "internalType": "address", "name": "from", "type": "address"}, {"indexed": True, "internalType": "address", "name": "to", "type": "address"}, {"indexed": False, "internalType": "uint256", "name": "value", "type": "uint256"}], "name": "Transfer", "type": "event"}
]

# Withdrawal limits — a stolen session/JWT should never be able to drain an
# unbounded amount instantly. Per-asset, since these are all ~1:1 USD-pegged
# stablecoins; tune via env without a redeploy.
MAX_WITHDRAWAL_PER_TX = float(os.getenv("CELO_MAX_WITHDRAWAL_PER_TX", "500"))
MAX_WITHDRAWAL_PER_DAY = float(os.getenv("CELO_MAX_WITHDRAWAL_PER_DAY", "2000"))

# --- 2. HELPER FUNCTIONS ---
def _send_admin_treasury_alert(subject: str, body: str) -> None:
    if not getattr(settings, "smtp_host", ""):
        return

    recipients_raw = getattr(settings, "admin_alert_emails", "") or ""
    recipients = [email.strip().lower() for email in recipients_raw.split(",") if email.strip()]
    if not recipients:
        return

    sender = getattr(settings, "smtp_from_email", "") or getattr(settings, "smtp_user", "")
    if not sender:
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    try:
        server = smtplib.SMTP(getattr(settings, "smtp_host", ""), int(getattr(settings, "smtp_port", 587) or 587), timeout=20)
        if bool(getattr(settings, "smtp_use_tls", True)):
            server.starttls()
        smtp_user = getattr(settings, "smtp_user", "")
        if smtp_user:
            server.login(smtp_user, getattr(settings, "smtp_password", ""))
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print(f"⚠️ Failed to send treasury alert email: {e}")


def get_treasury_address():
    pk = os.getenv("CELO_TREASURY_PK")
    if pk:
        try:
            clean_pk = pk if pk.startswith("0x") else f"0x{pk}"
            return w3.eth.account.from_key(clean_pk).address
        except Exception:
            pass
    return os.getenv("CELO_HOT_WALLET_ADDRESS", "0x6f7BeAb48EAfC47B89041899a35a0525a6A60F59")

# --- 3. PYDANTIC MODELS ---
class InitiateDepositReq(BaseModel):
    asset: str
    amount: float

class DepositStatusRes(BaseModel):
    status: str
    tx_hash: Optional[str] = None
    message: str = ""

class VerifyRequest(BaseModel):
    amount: float
    tx_hash: str
    asset: str
    counterparty: str = ""

class WithdrawReq(BaseModel):
    identifier: str
    amount: float
    asset: str
    otp_session_id: str = ""
    otp_code: str = ""
    totp_code: Optional[str] = None

# ======================================================================
# 🟢 SYNCHRONOUS AUTO-DETECTION ENDPOINTS
# ======================================================================

@router.post("/deposit/initiate")
async def initiate_deposit(req: InitiateDepositReq, db=Depends(get_db), current_user=Depends(get_verified_current_user)):
    if req.asset not in ASSET_CONTRACTS:
        raise HTTPException(status_code=400, detail="Unsupported Celo asset.")

    dep_id = f"DEP_{uuid.uuid4().hex[:8].upper()}"
    user_id = safe_object_id(current_user.get("_id"))

    # Each user gets their own deposit address (derived from CELO_MNEMONIC), so the
    # background watcher (workers/celo_deposit_watcher.py) can attribute an incoming
    # transfer to the right user by address instead of guessing from the amount.
    wallet_index = await get_or_create_celo_wallet_index(db, current_user.get("_id"))
    deposit_address = derive_celo_account(wallet_index).address

    await db["pending_deposits"].insert_one({
        "_id": dep_id,
        "userId": user_id,
        "asset": req.asset,
        "network": "celo",
        "amount": req.amount,
        "depositAddress": deposit_address,
        "walletIndex": wallet_index,
        "status": "listening",
        "createdAt": datetime.utcnow()
    })

    return {"deposit_id": dep_id, "address": deposit_address}


@router.get("/deposit/{dep_id}/status", response_model=DepositStatusRes)
async def get_deposit_status(dep_id: str, db=Depends(get_db), current_user=Depends(get_verified_current_user)):
    user_id = safe_object_id(current_user.get("_id")) # get current user id safely
    dep = await db["pending_deposits"].find_one({"_id": dep_id, "userId": user_id})

    if not dep:
        raise HTTPException(status_code=404, detail="Deposit session not found.")

    # The background watcher (workers/celo_deposit_watcher.py) owns detection and
    # crediting now that each deposit has its own address — this endpoint is just a
    # cheap DB read, kept as a fallback for clients that connect after missing the
    # real-time websocket push.
    if dep["status"] == "credited":
        return DepositStatusRes(status="credited", tx_hash=dep.get("tx_hash"), message="Funds credited!")

    return DepositStatusRes(status=dep["status"], message="Waiting for on-chain confirmation...")

# ======================================================================
# 🔵 MANUAL FALLBACK & WITHDRAWALS
# ======================================================================

@router.post("/on-ramp/verify", status_code=201)
async def verify_valora_deposit(req: VerifyRequest, db=Depends(get_db), current_user=Depends(get_verified_current_user)):
    user_id = safe_object_id(current_user.get("_id")) # 🟢 FIX
    
    if req.asset not in ASSET_CONTRACTS:
        raise HTTPException(status_code=400, detail="Unsupported Celo asset.")

    existing_tx = await db["ramp_entries"].find_one({"cardanoTxHash": req.tx_hash, "direction": "on"})
    if existing_tx:
        raise HTTPException(status_code=409, detail="This transaction hash has already been processed.")

    safe_hash = req.tx_hash.strip()
    if not safe_hash.startswith("0x"):
        safe_hash = "0x" + safe_hash

    def fetch_and_verify_receipt():
        try:
            receipt = w3.eth.get_transaction_receipt(safe_hash)
            if receipt.status != 1: return False, "Transaction failed or reverted."
                
            contract = w3.eth.contract(address=ASSET_CONTRACTS[req.asset], abi=ERC20_ABI)
            logs = contract.events.Transfer().process_receipt(receipt)
            
            treasury_addr = get_treasury_address().lower()
            decimals = 18 if req.asset == "cUSD" else 6
            expected_base_units = int(round(req.amount * (10 ** decimals) * 0.99))
            
            for log in logs:
                if log['args']['to'].lower() == treasury_addr and log['args']['value'] >= expected_base_units:
                    return True, "Valid"
            return False, "No valid transfers to the Treasury Address found."
        except Exception as e:
            return False, f"Blockchain query error: {str(e)}"

    is_valid, err_msg = await asyncio.to_thread(fetch_and_verify_receipt)
    
    if not is_valid:
        raise HTTPException(status_code=400, detail=err_msg)

    # 🟢 SAFELY UPDATES THE REAL UI WALLET
    await db["retail_wallets"].update_one(
        {"userId": user_id},
        {"$inc": {req.asset: req.amount}},
        upsert=True
    )

    now = datetime.utcnow()
    await db["ramp_entries"].insert_one({
        "_id": f"TRADE_{uuid.uuid4().hex[:8].upper()}",
        "direction": "on",
        "channel": "Opera MiniPay (Manual)",
        "fromAsset": req.asset,
        "toAsset": req.asset,
        "fromAmount": req.amount,
        "toAmount": req.amount,
        "status": "COMPLETED",
        "userId": user_id,
        "cardanoTxHash": safe_hash,
        "counterparty": req.counterparty or "MiniPay On-Chain",
        "date": now.strftime("%b %d, %Y"),
        "timeAgo": "Just now",
        "createdAt": now
    })
    
    return {"status": "success", "message": f"{req.amount} {req.asset} verified and credited!"}
@router.post("/withdraw")
async def withdraw_from_valora(req: WithdrawReq, db=Depends(get_db), current_user=Depends(get_verified_current_user)):
    import asyncio
    # Verified before anything else touches the balance.
    await verify_withdrawal_2fa(db, current_user, req.otp_session_id, req.otp_code, req.totp_code)

    # Safely convert user id to the same form used across other routes
    user_id = safe_object_id(current_user.get("_id"))

    onchain_asset = ONCHAIN_SETTLEMENT_ALIAS.get(req.asset, req.asset)
    if onchain_asset not in ASSET_CONTRACTS:
        raise HTTPException(status_code=400, detail="Unsupported Celo asset.")

    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Withdrawal amount must be positive.")

    await log_celo_audit_event(
        db, "withdrawal_requested",
        userId=str(user_id), asset=req.asset, amount=req.amount, destination=req.identifier,
    )

    if req.amount > MAX_WITHDRAWAL_PER_TX:
        await log_celo_audit_event(
            db, "withdrawal_blocked_limit",
            userId=str(user_id), asset=req.asset, amount=req.amount, destination=req.identifier,
            reason="per_tx_limit", limit=MAX_WITHDRAWAL_PER_TX,
        )
        raise HTTPException(status_code=400, detail=f"Withdrawals are capped at {MAX_WITHDRAWAL_PER_TX} {req.asset} per transaction.")

    day_ago = datetime.utcnow() - timedelta(hours=24)
    daily_totals = await db["celo_audit_log"].aggregate([
        {"$match": {"event": "withdrawal_broadcast", "userId": str(user_id), "asset": req.asset, "createdAt": {"$gte": day_ago}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]).to_list(length=1)
    already_withdrawn_today = daily_totals[0]["total"] if daily_totals else 0.0

    if already_withdrawn_today + req.amount > MAX_WITHDRAWAL_PER_DAY:
        await log_celo_audit_event(
            db, "withdrawal_blocked_limit",
            userId=str(user_id), asset=req.asset, amount=req.amount, destination=req.identifier,
            reason="daily_limit", limit=MAX_WITHDRAWAL_PER_DAY, alreadyWithdrawnToday=already_withdrawn_today,
        )
        raise HTTPException(
            status_code=400,
            detail=f"Daily withdrawal limit reached: up to {MAX_WITHDRAWAL_PER_DAY} {req.asset} per 24 hours. You've already withdrawn {already_withdrawn_today:.2f} {req.asset} today.",
        )

    # Sums/debits across every retail_wallets row for this user — see
    # wallet_utils.py. The old code here checked one row (find_one) but then
    # debited unconditionally with no $gte guard at all — worse than the
    # other withdrawal endpoints, since it could push a row negative outright.
    await debit_wallet(db, user_id, req.asset, req.amount)

    try:
        target_address = w3.to_checksum_address(req.identifier.strip())
    except Exception:
        await credit_wallet(db, user_id, req.asset, req.amount)
        raise HTTPException(status_code=400, detail="Invalid destination address. Must be a valid 0x format.")
    
    try:
        private_key = os.getenv("CELO_TREASURY_PK")
        if not private_key:
            raise ValueError("CELO_TREASURY_PK is missing in environment. Cannot sign transaction.")
            
        account = w3.eth.account.from_key(private_key if private_key.startswith("0x") else f"0x{private_key}")
        decimals = 18 if onchain_asset == "cUSD" else 6
        amount_base = int(req.amount * (10 ** decimals))

        # Broadcasts real USDC on-chain for a "USD" withdrawal (see
        # ONCHAIN_SETTLEMENT_ALIAS) while every retail_wallets read/write in
        # this function still uses req.asset ("USD"), so the internal ledger
        # stays correctly labeled.
        contract = w3.eth.contract(address=ASSET_CONTRACTS[onchain_asset], abi=ERC20_ABI)
        
        # 🟢 CHECK TREASURY GAS BALANCE BEFORE ATTEMPTING TRANSFER
        def check_gas_balance():
            try:
                celo_balance_wei = w3.eth.get_balance(account.address)
                gas_price = w3.eth.gas_price
                estimated_gas = 150000
                required_wei = gas_price * estimated_gas
                
                print(f"🔋 Treasury CELO balance: {celo_balance_wei / 1e18:.6f} CELO")
                print(f"⛽ Gas required: {required_wei / 1e18:.6f} CELO (gas: {estimated_gas}, price: {gas_price})")
                
                if celo_balance_wei < required_wei:
                    have_celo = celo_balance_wei / 1e18
                    need_celo = required_wei / 1e18
                    raise ValueError(f"Treasury insufficient CELO for gas. Have: {have_celo:.6f} CELO, Need: {need_celo:.6f} CELO")
                
                return True
            except ValueError:
                raise
            except Exception as e:
                print(f"⚠️ Error checking gas balance: {e}")
                return True  # Continue anyway if we can't check
        
        await asyncio.to_thread(check_gas_balance)

        # 🟢 CHECK TREASURY TOKEN BALANCE BEFORE ATTEMPTING TRANSFER — the gas
        # check above only guards CELO for fees; it says nothing about whether
        # the treasury actually holds enough of the asset being withdrawn.
        # Without this, a withdrawal request the treasury can't cover still
        # got broadcast, reverted on-chain, and used to be recorded as a
        # completed withdrawal anyway (see the receipt-wait fix below).
        def check_token_balance():
            treasury_balance_base = contract.functions.balanceOf(account.address).call()
            have = treasury_balance_base / (10 ** decimals)
            print(f"💰 Treasury {onchain_asset} balance: {have:.6f} (need {req.amount:.6f})")
            if treasury_balance_base < amount_base:
                raise ValueError(
                    f"Treasury insufficient {onchain_asset} balance. Have: {have:.6f} {onchain_asset}, Need: {req.amount:.6f} {onchain_asset}"
                )

        await asyncio.to_thread(check_token_balance)

        def execute_tx():
            nonce = w3.eth.get_transaction_count(account.address)
            tx = contract.functions.transfer(target_address, amount_base).build_transaction({
                'chainId': 42220, # Hardcoded chain ID from earlier to ensure safety
                'gas': 150000,
                'gasPrice': w3.eth.gas_price,
                'nonce': nonce,
            })
            signed_tx = w3.eth.account.sign_transaction(tx, account.key)
            raw_tx = getattr(signed_tx, 'raw_transaction', getattr(signed_tx, 'rawTransaction', None))
            sent_hash = w3.to_hex(w3.eth.send_raw_transaction(raw_tx))
            # A successful broadcast only means the network accepted the tx
            # into the mempool — it says nothing about whether it actually
            # executed. Wait for the receipt and check status before telling
            # anyone this withdrawal completed; otherwise a revert (e.g. the
            # balance-exceeded case this endpoint hit in production) still
            # gets recorded as status: COMPLETED with a real tx hash attached,
            # even though zero tokens moved.
            receipt = w3.eth.wait_for_transaction_receipt(sent_hash, timeout=60)
            if receipt.get("status") != 1:
                raise RuntimeError(f"Transaction reverted on-chain: {sent_hash}")
            return sent_hash

        tx_hex = await asyncio.to_thread(execute_tx)

    except Exception as e:
        # Refund user if broadcast fails
        await credit_wallet(db, user_id, req.asset, req.amount)
        print(f"⚠️ Celo Withdrawal Error: {e}")
        await log_celo_audit_event(
            db, "withdrawal_failed",
            userId=str(user_id), asset=req.asset, amount=req.amount, destination=req.identifier,
            error=str(e),
        )

        # Customer-facing copy must never include the raw exception text —
        # it leaks internal treasury/liquidity details ("Treasury insufficient
        # USDT balance. Have: 2.68...") to the user. The full detail still
        # goes to celo_audit_log above and the admin alert emails below;
        # this is deliberately generic regardless of the underlying cause.
        await notify_user(
            db, user_id, "withdrawal", "error",
            "Withdrawal failed",
            f"Your withdrawal of {req.amount:g} {req.asset} failed and was refunded. Please try again shortly, or contact support if this continues.",
            extra={"asset": req.asset, "amount": req.amount},
        )

        # Provide more helpful error messages
        error_str = str(e)
        if "Treasury insufficient" in error_str and "balance" in error_str:
            _send_admin_treasury_alert(
                f"[URGENT] Treasury balance too low to process a withdrawal",
                f"User {user_id} tried to withdraw {req.amount} {req.asset} to {req.identifier}.\n\n"
                f"{error_str}\n\n"
                f"The withdrawal was blocked and the user's balance was refunded automatically. "
                f"Please top up the treasury wallet ({get_treasury_address()}) so future withdrawals go through.",
            )
            raise HTTPException(status_code=503, detail="System is temporarily unable to process withdrawals: treasury balance is low. Support has been notified — please try again shortly.")
        elif "reverted on-chain" in error_str:
            _send_admin_treasury_alert(
                f"[URGENT] Withdrawal reverted on-chain after broadcast",
                f"User {user_id}'s withdrawal of {req.amount} {req.asset} to {req.identifier} was broadcast but "
                f"reverted on-chain.\n\n{error_str}\n\nThe user's balance was refunded automatically. Please investigate the treasury wallet.",
            )
            raise HTTPException(status_code=502, detail="Withdrawal failed to confirm on-chain and was refunded. Please try again or contact support.")
        elif "insufficient" in error_str.lower() and "celo" in error_str.lower():
            raise HTTPException(status_code=503, detail="System is temporarily unable to process withdrawals. Treasury CELO balance is low. Please try again later.")
        elif "insufficient funds for gas" in error_str:
            raise HTTPException(status_code=503, detail="System is temporarily unable to process withdrawals. Insufficient transaction fees. Please try again later.")
        else:
            raise HTTPException(status_code=502, detail=f"Blockchain transfer failed: {error_str}")

    now = datetime.utcnow()
    import uuid
    await db["ramp_entries"].insert_one({
        "_id": f"TRADE_{uuid.uuid4().hex[:8].upper()}",
        "direction": "off", "channel": "Opera MiniPay", "fromAsset": req.asset, "toAsset": req.asset,
        "fromAmount": req.amount, "toAmount": req.amount, "rate": 1.0, "fee": 0.0,
        "counterparty": req.identifier, "status": "COMPLETED",
        "cardanoTxHash": tx_hex, "cardanoAddress": target_address,
        # Canonical fields for the tx-hash/explorer-link UI — "cardanoTxHash"
        # above is a legacy misnomer (this is always a Celo tx) kept only so
        # older readers of this collection don't break.
        "txHash": tx_hex, "network": "celo",
        "userId": user_id, "createdAt": now, "date": now.strftime("%b %d, %Y"), "timeAgo": "Just now"
    })

    await log_celo_audit_event(
        db, "withdrawal_broadcast",
        userId=str(user_id), asset=req.asset, amount=req.amount, destination=target_address, txHash=tx_hex,
    )

    await notify_user(
        db, user_id, "withdrawal", "success",
        "Withdrawal completed",
        f"{req.amount:g} {req.asset} was sent to your wallet.",
        extra={"asset": req.asset, "amount": req.amount, "txHash": tx_hex},
    )

    return {"status": "success", "message": f"{req.amount} {req.asset} sent to your wallet!", "tx_hash": tx_hex}
