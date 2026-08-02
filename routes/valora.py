import os
import uuid
import asyncio
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from database import get_db
from routes.auth import get_current_user
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
    "USDT": w3.to_checksum_address("0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e")  
}

ERC20_ABI = [
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"anonymous": False, "inputs": [{"indexed": True, "internalType": "address", "name": "from", "type": "address"}, {"indexed": True, "internalType": "address", "name": "to", "type": "address"}, {"indexed": False, "internalType": "uint256", "name": "value", "type": "uint256"}], "name": "Transfer", "type": "event"}
]

# --- 2. HELPER FUNCTIONS ---
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

# ======================================================================
# 🟢 SYNCHRONOUS AUTO-DETECTION ENDPOINTS
# ======================================================================

@router.post("/deposit/initiate")
async def initiate_deposit(req: InitiateDepositReq, db=Depends(get_db), current_user=Depends(get_current_user)):
    if req.asset not in ASSET_CONTRACTS:
        raise HTTPException(status_code=400, detail="Unsupported Celo asset.")
        
    dep_id = f"DEP_{uuid.uuid4().hex[:8].upper()}"
    user_id = safe_object_id(current_user.get("_id")) # 🟢 FIX
    
    await db["pending_deposits"].insert_one({
        "_id": dep_id,
        "userId": user_id,
        "asset": req.asset,
        "network": "celo",
        "amount": req.amount,
        "status": "listening",
        "createdAt": datetime.utcnow()
    })
    
    return {"deposit_id": dep_id, "address": get_treasury_address()}


@router.get("/deposit/{dep_id}/status", response_model=DepositStatusRes)
async def get_deposit_status(dep_id: str, db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = safe_object_id(current_user.get("_id")) # 🟢 FIX
    dep = await db["pending_deposits"].find_one({"_id": dep_id, "userId": user_id})
    
    if not dep: 
        raise HTTPException(status_code=404, detail="Deposit session not found.")
        
    if dep["status"] == "credited":
        return DepositStatusRes(status="credited", tx_hash=dep.get("tx_hash"), message="Funds credited!")

    # Fetch already processed hashes
    recent_txs = await db["ramp_entries"].find({
        "direction": "on",
        "fromAsset": dep["asset"],
        "status": "COMPLETED"
    }).to_list(length=100)
    
    used_hashes = set()
    for tx in recent_txs:
        h = tx.get("cardanoTxHash", "").lower()
        if not h.startswith("0x"): h = "0x" + h
        used_hashes.add(h)

    def scan_celo_logs():
        treasury = get_treasury_address().lower()
        decimals = 18 if dep["asset"] == "cUSD" else 6
        expected_val = int(round(dep["amount"] * (10 ** decimals) * 0.99))
        
        current_block = w3.eth.block_number
        start_block = max(current_block - 1000, 0) # 🟢 Increased to 1000 blocks (80 mins)
        
        contract = w3.eth.contract(address=ASSET_CONTRACTS[dep["asset"]], abi=ERC20_ABI)
        transfer_topic = w3.to_hex(w3.keccak(text="Transfer(address,address,uint256)"))
        
        try:
            logs = w3.eth.get_logs({
                "fromBlock": start_block,
                "toBlock": "latest",
                "address": ASSET_CONTRACTS[dep["asset"]],
                "topics": [transfer_topic]
            })
            
            print(f"🔍 [Scanner] Found {len(logs)} {dep['asset']} transfers. Looking for {dep['amount']} to {treasury}")
            
            for log in logs:
                try:
                    parsed_log = contract.events.Transfer().process_log(log)
                    to_addr = parsed_log['args']['to'].lower()
                    value = parsed_log['args']['value']
                    
                    tx_hash = parsed_log['transactionHash'].hex().lower()
                    if not tx_hash.startswith("0x"):
                        tx_hash = "0x" + tx_hash

                    if tx_hash in used_hashes:
                        continue 
                    
                    if to_addr == treasury and value >= expected_val:
                        print(f"💸 [Scanner] WOW! Found transfer to Treasury! Hash: {tx_hash}")
                        return tx_hash
                            
                except Exception:
                    pass
                            
        except Exception as e:
            print(f"⚠️ Celo get_logs error: {e}")
            
        return None

    try:
        found_hash = await asyncio.to_thread(scan_celo_logs)
        
        if found_hash:
            clean_hash = found_hash if found_hash.startswith("0x") else f"0x{found_hash}"
            
            await db["pending_deposits"].update_one(
                {"_id": dep_id}, 
                {"$set": {"status": "credited", "tx_hash": clean_hash}}
            )
            
            # 🟢 SAFELY UPDATES THE REAL UI WALLET
            await db["retail_wallets"].update_one(
                {"userId": user_id}, 
                {"$inc": {dep["asset"]: dep["amount"]}}, 
                upsert=True
            )
            
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
                "cardanoTxHash": clean_hash,
                "createdAt": now,
                "date": now.strftime("%b %d, %Y"),
                "timeAgo": "Just now"
            })
            
            return DepositStatusRes(status="credited", tx_hash=clean_hash, message="Funds credited!")
            
    except Exception as e:
        print(f"⚠️ Celo auto-detect thread error: {e}")

    return DepositStatusRes(status="listening", message="Scanning last 1000 blocks...")

# ======================================================================
# 🔵 MANUAL FALLBACK & WITHDRAWALS
# ======================================================================

@router.post("/on-ramp/verify", status_code=201)
async def verify_valora_deposit(req: VerifyRequest, db=Depends(get_db), current_user=Depends(get_current_user)):
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