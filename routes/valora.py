import os
import uuid
import asyncio
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from database import get_db

from dotenv import load_dotenv

load_dotenv(override=True)

router = APIRouter(prefix="/api/valora", tags=["Valora & Celo"])

# ==========================================
# 1. DYNAMIC NETWORK CONFIGURATION
# ==========================================
CELO_RPC = os.getenv("CELO_RPC_URL", "https://forno.celo.org")

# Auto-detect Mainnet vs Testnet
if "forno.celo.org" in CELO_RPC or "mainnet" in CELO_RPC:
    CHAIN_ID = 42220
    USDC_ADDRESS = "0x07865c6E87B9F70255377e024ef6629E264Ec76"
    CUSD_ADDRESS = "0x765DE816845861e75A25fCA122abb6898B8B1282a"
    NETWORK_NAME = "Celo Mainnet"
elif "alfajores" in CELO_RPC or "testnet" in CELO_RPC:
    CHAIN_ID = 44787
    USDC_ADDRESS = "0x874069Fa1Eb16D44d622F2e0Ca25eeA172369bC1"
    CUSD_ADDRESS = "0x874069Fa1Eb16D44d622F2e0Ca25eeA172369bC1"
    NETWORK_NAME = "Celo Alfajores (Testnet)"
else:
    CHAIN_ID = 42220
    USDC_ADDRESS = "0x07865c6E87B9F70255377e024ef6629E264Ec76"
    CUSD_ADDRESS = "0x765DE816845861e75A25fCA122abb6898B8B1282a"
    NETWORK_NAME = "Celo Mainnet"

# Initialize Web3
w3 = Web3(Web3.HTTPProvider(CELO_RPC))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

# ERC20 ABI (Standard transfer + Event logging for the scanner)
ERC20_ABI = [
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"anonymous": False, "inputs": [{"indexed": True, "internalType": "address", "name": "from", "type": "address"}, {"indexed": True, "internalType": "address", "name": "to", "type": "address"}, {"indexed": False, "internalType": "uint256", "name": "value", "type": "uint256"}, {"indexed": False, "internalType": "bytes", "name": "data", "type": "bytes"}], "name": "Transfer", "type": "event"}
]

# ==========================================
# 2. DATA MODELS
# ==========================================
class WithdrawReq(BaseModel):
    identifier: str
    amount: float

class LinkReq(BaseModel):
    phone_number: str
    valora_address: str

class DepositMemoResponse(BaseModel):
    treasury_address: str
    memo: str
    network: str
    asset: str

# ==========================================
# 3. PHONE NUMBER LINKING
# ==========================================
@router.post("/link-phone")
async def link_phone_to_valora(req: LinkReq, db=Depends(get_db)):
    """Maps a user's Phone Number to their Valora 0x Address."""
    if not w3.is_address(req.valora_address):
        raise HTTPException(status_code=400, detail="Invalid Celo Address format.")
    
    checksum_address = w3.to_checksum_address(req.valora_address)
    
    await db["valora_mappings"].update_one(
        {"phone_number": req.phone_number},
        {"$set": {"valora_address": checksum_address, "updated_at": datetime.utcnow()}},
        upsert=True
    )
    return {"status": "success", "message": f"Linked {req.phone_number} to {checksum_address}"}

# ==========================================
# 4. DEPOSIT: GENERATE MEMO & TREASURY INFO
# ==========================================
@router.get("/deposit-details", response_model=DepositMemoResponse)
async def get_deposit_details(db=Depends(get_db)):
    """
    Returns the Treasury Address and generates a unique Memo (Reference) 
    for the user to include when sending funds from Valora.
    """
    # Try to use existing corridor treasury, fallback to .env direct key
    try:
        from Brain_Engine.celo_integrations import corridor_api
        treasury_address = w3.to_checksum_address(corridor_api.treasury_wallet.address)
    except Exception:
        private_key = os.getenv("CELO_TREASURY_PK")
        if not private_key:
            raise HTTPException(status_code=500, detail="Treasury wallet not configured.")
        if not private_key.startswith("0x"):
            private_key = f"0x{private_key}"
        account = w3.eth.account.from_key(private_key)
        treasury_address = account.address

    # Generate a temporary memo for unauthenticated users, or user-specific if logged in
    # In production, you would pass the actual user_id here from auth
    memo = f"MESH-ANON-{uuid.uuid4().hex[:8].upper()}"

    return {
        "treasury_address": treasury_address,
        "memo": memo,
        "network": NETWORK_NAME,
        "asset": "USDC/cUSD"
    }

# ==========================================
# 5. WITHDRAWAL (OFF-RAMP)
# ==========================================
@router.post("/withdraw")
async def withdraw_from_valora(req: WithdrawReq, db=Depends(get_db)):
    """Executes a Web3 transaction, moving stablecoins from Treasury to User's Valora App."""
    
    # 1. Resolve Address (Phone Number vs 0x)
    target_address = req.identifier.strip()
    if not target_address.startswith("0x"):
        mapping = await db["valora_mappings"].find_one({"phone_number": req.identifier})
        if not mapping:
            raise HTTPException(status_code=404, detail="Phone number not linked. Please provide a 0x address.")
        target_address = mapping["valora_address"]
        
    if not w3.is_address(target_address):
        raise HTTPException(status_code=400, detail="Invalid destination address.")
        
    target_address = w3.to_checksum_address(target_address)
    
    # 2. Unlock Treasury
    try:
        from Brain_Engine.celo_integrations import corridor_api
        account = corridor_api.treasury_wallet
    except Exception:
        private_key = os.getenv("CELO_TREASURY_PK")
        if not private_key: raise HTTPException(status_code=500, detail="Treasury PK missing.")
        if not private_key.startswith("0x"): private_key = f"0x{private_key}"
        account = w3.eth.account.from_key(private_key)
    
    # 3. Build & Send Transaction
    amount_in_wei = w3.to_wei(req.amount, 'ether')
    contract = w3.eth.contract(address=w3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
    
    try:
        nonce = w3.eth.get_transaction_count(account.address)
        tx = contract.functions.transfer(target_address, amount_in_wei).build_transaction({
            'chainId': CHAIN_ID,
            'gas': 150000,
            'gasPrice': w3.eth.gas_price,
            'nonce': nonce,
        })
        
        signed_tx = w3.eth.account.sign_transaction(tx, account.key)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        tx_hex = tx_hash.hex()
        
    except Exception as e:
        error_msg = str(e)
        if "insufficient funds" in error_msg.lower():
            raise HTTPException(status_code=400, detail="Treasury lacks USDC/cUSD balance or CELO for gas.")
        raise HTTPException(status_code=502, detail=f"Celo Error: {error_msg}")

    # 4. Log to DB
    await db["ramp_entries"].insert_one({
        "direction": "off", "channel": "Valora Wallet", "fromAsset": "USDC", "toAsset": "USDC",
        "fromAmount": req.amount, "toAmount": req.amount, "rate": 1.0, "fee": 0.0,
        "counterparty": req.identifier, "status": "COMPLETED", 
        "cardanoTxHash": tx_hex, "cardanoAddress": target_address,
        "userId": "system", "createdAt": datetime.utcnow(),
    })

    return {"status": "success", "message": "Funds sent to Valora!", "tx_hash": tx_hex}

# ==========================================
# 6. THE DEPOSIT SCANNER (Background Worker)
# ==========================================
async def scan_for_deposits(db):
    """
    Scans the Celo blockchain for new incoming transactions to the Treasury.
    Looks for the "MESH-" prefix in the transaction memo to credit the user.
    """
    try:
        from Brain_Engine.celo_integrations import corridor_api
        treasury_address = w3.to_checksum_address(corridor_api.treasury_wallet.address)
    except Exception:
        private_key = os.getenv("CELO_TREASURY_PK")
        if not private_key: return 0
        if not private_key.startswith("0x"): private_key = f"0x{private_key}"
        account = w3.eth.account.from_key(private_key)
        treasury_address = account.address

    # Get last scanned block from DB
    state = await db["system_state"].find_one_and_update(
        {"_id": "celo_deposit_scanner"},
        {"$setOnInsert": {"last_block": w3.eth.block_number - 100}},
        upsert=True,
        return_document=True
    )
    last_block = state.get("last_block", w3.eth.block_number - 100)
    current_block = w3.eth.block_number
    
    if current_block <= last_block:
        return 0

    print(f"🔍 [Celo Scanner] Checking blocks {last_block} to {current_block}...")
    
    contract = w3.eth.contract(address=treasury_address, abi=ERC20_ABI)
    logs = contract.events.Transfer().get_logs(from_block=last_block + 1, to_block=current_block)
    
    processed = 0
    for log in logs:
        tx_hash = log["transactionHash"].hex()
        
        # Prevent double-processing
        exists = await db["ramp_entries"].find_one({"cardanoTxHash": tx_hash})
        if exists: continue
            
        sender = log["args"]["from"]
        raw_amount = log["args"]["value"]
        memo_bytes = log["args"].get("data", b'').decode('utf-8', errors='ignore') or ""
        
        # Did the user include the "MESH-" reference?
        if memo_bytes.startswith("MESH-"):
            user_id = memo_bytes.split("-")[1]
            
            print(f"  ✅ Deposit matched! Memo: {memo_bytes} -> Crediting {user_id}")
            amount_to_credit = float(w3.from_wei(raw_amount, 'ether'))
            
            # Credit internal MongoDB balance
            await db["user_wallets"].update_one(
                {"_id": user_id},
                {"$inc": {"balances.USDC": amount_to_credit, "balances.cUSD": amount_to_credit}},
                upsert=True
            )
            
            # Save to history
            await db["ramp_entries"].insert_one({
                "direction": "on", "channel": "Valora Wallet", "fromAsset": "USDC", "toAsset": "USDC",
                "fromAmount": amount_to_credit, "toAmount": amount_to_credit,
                "rate": 1.0, "fee": 0.0, "counterparty": sender,
                "status": "COMPLETED", "cardanoTxHash": tx_hash,
                "cardanoAddress": sender, "userId": user_id, "createdAt": datetime.utcnow(),
            })
            processed += 1
            
        else:
            print(f"  ⚠️ Received funds but NO MESH- memo. Flagging for manual review.")
            await db["ramp_entries"].insert_one({
                "direction": "on", "channel": "Valora Wallet", "fromAsset": "USDC", "toAsset": "USDC",
                "fromAmount": float(w3.from_wei(raw_amount, 'ether')),
                "toAmount": float(w3.from_wei(raw_amount, 'ether')),
                "rate": 1.0, "fee": 0.0, "counterparty": sender,
                "status": "PENDING_MANUAL_REVIEW", 
                "cardanoTxHash": tx_hash, "cardanoAddress": sender, "userId": "unknown",
                "createdAt": datetime.utcnow(),
            })

    # Update scanner position
    await db["system_state"].update_one(
        {"_id": "celo_deposit_scanner"},
        {"$set": {"last_block": current_block, "last_run": datetime.utcnow()}}
    )
    
    return processed


async def run_scanner_loop():
    """Infinite background loop that scans for deposits every 10 seconds."""
    while True:
        try:
            db = get_db().__next__()
            await scan_for_deposits(db)
        except Exception as e:
            print(f"Scanner error: {e}")
        await asyncio.sleep(10)


# ==========================================
# 7. START SCANNER ON APP BOOT
# ==========================================
@router.on_event("startup")
async def startup_scanner():
    print(f"🚀 Starting Celo Deposit Scanner on {NETWORK_NAME}...")
    loop = asyncio.get_event_loop()
    loop.create_task(run_scanner_loop())