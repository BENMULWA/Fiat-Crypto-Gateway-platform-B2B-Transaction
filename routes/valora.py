import os
import uuid
import asyncio
import time
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from database import get_db
from routes.auth import get_current_user
from dotenv import load_dotenv

load_dotenv(override=True)

router = APIRouter(prefix="/api/valora", tags=["Celo Wallets"])

# --- 1. WEB3 & CELO CONFIGURATION ---
CELO_RPC = os.getenv("CELO_RPC_URL", "https://rpc.ankr.com/celo")
CHAIN_ID = 42220

w3 = Web3(Web3.HTTPProvider(CELO_RPC, request_kwargs={'timeout': 10}))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

ASSET_CONTRACTS = {
    "cUSD": w3.to_checksum_address("0x765DE816845861e75A25fCA122bb6898B8B1282a"), 
    "USDC": w3.to_checksum_address("0xcebA9300f2b948710d2653dD7B07f33A8B32118C"), 
    "USDT": w3.to_checksum_address("0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e")  
}

# Minimal ABI to decode transfer logs
ERC20_ABI = [
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"anonymous": False, "inputs": [{"indexed": True, "internalType": "address", "name": "from", "type": "address"}, {"indexed": True, "internalType": "address", "name": "to", "type": "address"}, {"indexed": False, "internalType": "uint256", "name": "value", "type": "uint256"}], "name": "Transfer", "type": "event"}
]

# --- 2. HELPER FUNCTIONS ---
def get_treasury_address():
    pk = os.getenv("CELO_TREASURY_PK")
    if not pk: return "0x0000000000000000000000000000000000000000"
    if not pk.startswith("0x"): pk = f"0x{pk}"
    return w3.eth.account.from_key(pk).address


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

class DepositMemoResponse(BaseModel):
    treasury_address: str
    memo: str
    network: str
    asset: str


# ======================================================================
# 🟢 NEW: SYNCHRONOUS AUTO-DETECTION ENDPOINTS
# ======================================================================

@router.post("/deposit/initiate")
async def initiate_deposit(req: InitiateDepositReq, db=Depends(get_db), current_user=Depends(get_current_user)):
    if req.asset not in ASSET_CONTRACTS:
        raise HTTPException(status_code=400, detail="Unsupported Celo asset.")
        
    dep_id = f"DEP_{uuid.uuid4().hex[:8].upper()}"
    
    await db["pending_deposits"].insert_one({
        "_id": dep_id,
        "userId": current_user.get("_id"),
        "asset": req.asset,
        "network": "celo",
        "amount": req.amount,
        "status": "listening",
        "createdAt": datetime.utcnow()
    })
    
    return {"deposit_id": dep_id, "address": get_treasury_address()}


@router.get("/deposit/{dep_id}/status", response_model=DepositStatusRes)
async def get_deposit_status(dep_id: str, db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = current_user.get("_id")
    dep = await db["pending_deposits"].find_one({"_id": dep_id, "userId": user_id})
    
    if not dep: 
        raise HTTPException(status_code=404, detail="Deposit session not found.")
        
    # If already credited, return immediately without hitting the RPC
    if dep["status"] == "credited":
        return DepositStatusRes(status="credited", tx_hash=dep.get("tx_hash"), message="Funds credited!")

    def scan_celo_blocks():
        treasury = get_treasury_address().lower()
        decimals = 18 if dep["asset"] == "cUSD" else 6
        expected_val = int(dep["amount"] * (10 ** decimals))
        contract = w3.eth.contract(address=ASSET_CONTRACTS[dep["asset"]], abi=ERC20_ABI)
        
        current_block = w3.eth.block_number
        # Scan last 5 blocks (approx 15 seconds of Celo history)
        for i in range(current_block, max(current_block - 5, 0), -1):
            block = w3.eth.get_block(i, full_transactions=True)
            for tx in block.transactions:
                try:
                    # Check if tx interacted with our specific asset contract
                    if tx.to and tx.to.lower() == ASSET_CONTRACTS[dep["asset"]].lower():
                        receipt = w3.eth.get_transaction_receipt(tx.hash)
                        logs = contract.events.Transfer().process_receipt(receipt)
                        for log in logs:
                            if log['args']['to'].lower() == treasury and log['args']['value'] >= expected_val:
                                return tx.hash.hex()
                except Exception:
                    continue
        return None

    try:
        # Run synchronous web3 scanner in FastAPI's threadpool so it doesn't block the server
        found_hash = await asyncio.to_thread(scan_celo_blocks)
        
        if found_hash:
            # 1. Update pending deposit
            await db["pending_deposits"].update_one(
                {"_id": dep_id}, 
                {"$set": {"status": "credited", "tx_hash": found_hash}}
            )
            # 2. Credit user wallet
            await db["retail_wallets"].update_one(
                {"userId": user_id}, 
                {"$inc": {dep["asset"]: dep["amount"]}}, 
                upsert=True
            )
            # 3. Log to main ledger
            now = datetime.utcnow()
            await db["ramp_entries"].insert_one({
                "_id": f"TRADE_{uuid.uuid4().hex[:8].upper()}",
                "direction": "on",
                "channel": "Celo Auto-Detect",
                "fromAsset": dep["asset"],
                "toAsset": dep["asset"],
                "fromAmount": dep["amount"],
                "toAmount": dep["amount"],
                "status": "COMPLETED",
                "userId": user_id,
                "cardanoTxHash": found_hash,
                "createdAt": now,
                "date": now.strftime("%b %d, %Y"),
                "timeAgo": "Just now"
            })
            
            return DepositStatusRes(status="credited", tx_hash=found_hash, message="Funds credited!")
            
    except Exception as e:
        print(f"⚠️ Celo scan error: {e}")

    return DepositStatusRes(status="listening", message="Scanning last 5 blocks...")


# ======================================================================
# 🔵 LEGACY ENDPOINTS (Manual Fallback & Withdrawals)
# ======================================================================

@router.get("/deposit-details", response_model=DepositMemoResponse)
async def get_deposit_details():
    """Legacy endpoint used by frontend to show address before 'Start Listener' is clicked."""
    return {
        "treasury_address": get_treasury_address(),
        "memo": f"MESH-ANON-{uuid.uuid4().hex[:8].upper()}", # Note: Memos aren't actually used on Celo, but kept for UI consistency
        "network": "Celo Mainnet",
        "asset": "USDC/USDT/cUSD"
    }


@router.post("/on-ramp/verify", status_code=201)
async def verify_valora_deposit(req: VerifyRequest, db=Depends(get_db), current_user=Depends(get_current_user)):
    """Legacy manual Tx Hash verification fallback."""
    user_id = current_user.get("_id")
    
    if req.asset not in ASSET_CONTRACTS:
        raise HTTPException(status_code=400, detail="Unsupported Celo asset.")

    existing_tx = await db["ramp_entries"].find_one({"cardanoTxHash": req.tx_hash, "direction": "on"})
    if existing_tx:
        raise HTTPException(status_code=409, detail="This transaction hash has already been processed.")

    def fetch_and_verify_receipt():
        for attempt in range(3):
            try:
                receipt = w3.eth.get_transaction_receipt(req.tx_hash)
                if receipt.status != 1:
                    return False, "Transaction failed or reverted on the blockchain."
                    
                contract = w3.eth.contract(address=ASSET_CONTRACTS[req.asset], abi=ERC20_ABI)
                logs = contract.events.Transfer().process_receipt(receipt)
                
                treasury_addr = get_treasury_address().lower()
                decimals = 18 if req.asset == "cUSD" else 6
                expected_base_units = int(req.amount * (10 ** decimals))
                
                for log in logs:
                    if log['args']['to'].lower() == treasury_addr:
                        if log['args']['value'] >= expected_base_units:
                            return True, "Valid"
                            
                return False, f"Funds were not sent to the Treasury or amount was less than {req.amount} {req.asset}."
                
            except Exception as e:
                err_str = str(e)
                if "Connection" in err_str and attempt < 2:
                    time.sleep(1.5)
                    continue
                return False, f"Blockchain query error: {err_str}"
                
        return False, "Failed to connect to Celo RPC after 3 attempts."

    is_valid, err_msg = await asyncio.to_thread(fetch_and_verify_receipt)
    
    if not is_valid:
        raise HTTPException(status_code=400, detail=err_msg)

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
        "cardanoTxHash": req.tx_hash,
        "counterparty": req.counterparty or "MiniPay On-Chain",
        "date": now.strftime("%b %d, %Y"),
        "timeAgo": "Just now",
        "createdAt": now
    })
    
    return {"status": "success", "message": f"{req.amount} {req.asset} verified on Celo and credited!"}


@router.post("/withdraw")
async def withdraw_from_valora(req: WithdrawReq, db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = current_user.get("_id")

    if req.asset not in ASSET_CONTRACTS:
        raise HTTPException(status_code=400, detail="Unsupported Celo asset.")

    user_wallet = await db["retail_wallets"].find_one({"userId": user_id})
    current_bal = float(user_wallet.get(req.asset, 0.0)) if user_wallet else 0.0
    
    if current_bal < req.amount:
        raise HTTPException(status_code=400, detail=f"Insufficient {req.asset} balance. You have {current_bal}.")

    # Deduct funds internally first (Rollback if blockchain fails)
    await db["retail_wallets"].update_one(
        {"userId": user_id},
        {"$inc": {req.asset: -req.amount}}
    )

    target_address = req.identifier.strip()
    if not w3.is_address(target_address):
        await db["retail_wallets"].update_one({"userId": user_id}, {"$inc": {req.asset: req.amount}})
        raise HTTPException(status_code=400, detail="Invalid Celo destination address.")
        
    target_address = w3.to_checksum_address(target_address)
    
    try:
        private_key = os.getenv("CELO_TREASURY_PK")
        if not private_key:
            raise ValueError("CELO_TREASURY_PK is missing in environment. Cannot sign transaction.")
            
        account = w3.eth.account.from_key(private_key if private_key.startswith("0x") else f"0x{private_key}")
        decimals = 18 if req.asset == "cUSD" else 6
        amount_base = int(req.amount * (10 ** decimals))

        contract = w3.eth.contract(address=ASSET_CONTRACTS[req.asset], abi=ERC20_ABI)
        
        def execute_tx():
            nonce = w3.eth.get_transaction_count(account.address)
            tx = contract.functions.transfer(target_address, amount_base).build_transaction({
                'chainId': CHAIN_ID,
                'gas': 150000,
                'gasPrice': w3.eth.gas_price,
                'nonce': nonce,
            })
            signed_tx = w3.eth.account.sign_transaction(tx, account.key)
            raw_tx = getattr(signed_tx, 'raw_transaction', getattr(signed_tx, 'rawTransaction', None))
            return w3.to_hex(w3.eth.send_raw_transaction(raw_tx))

        tx_hex = await asyncio.to_thread(execute_tx)

    except Exception as e:
        # Refund user if broadcast fails
        await db["retail_wallets"].update_one({"userId": user_id}, {"$inc": {req.asset: req.amount}})
        raise HTTPException(status_code=502, detail=f"Blockchain transfer failed: {str(e)}")

    now = datetime.utcnow()
    await db["ramp_entries"].insert_one({
        "_id": f"TRADE_{uuid.uuid4().hex[:8].upper()}",
        "direction": "off", "channel": "Opera MiniPay", "fromAsset": req.asset, "toAsset": req.asset,
        "fromAmount": req.amount, "toAmount": req.amount, "rate": 1.0, "fee": 0.0,
        "counterparty": req.identifier, "status": "COMPLETED", 
        "cardanoTxHash": tx_hex, "cardanoAddress": target_address,
        "userId": user_id, "createdAt": now, "date": now.strftime("%b %d, %Y"), "timeAgo": "Just now"
    })

    return {"status": "success", "message": f"{req.amount} {req.asset} sent to your wallet!", "tx_hash": tx_hex}