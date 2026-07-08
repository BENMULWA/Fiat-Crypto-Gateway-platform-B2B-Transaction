import os
import asyncio
import json
from web3 import Web3

# Web3 v7 renamed the middleware, so we use a try/except to handle both versions!
try:
    from web3.middleware import geth_poa_middleware
except ImportError:
    from web3.middleware import ExtraDataToPOAMiddleware as geth_poa_middleware

from dotenv import load_dotenv
from eth_account import Account

load_dotenv()
Account.enable_unaudited_hdwallet_features()

# Minimal ABI required to execute an ERC-20 Token Transfer on the blockchain
ERC20_ABI = json.loads('[{"constant":false,"inputs":[{"name":"_to","type":"address"},{"name":"_value","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"type":"function"}]')

class CorridorIntegrations:
    def __init__(self):
        
        # We enforce live mode to hit the actual blockchain
        self.live_mode = True 
        
        # Connect to Celo Mainnet via Forno RPC
        self.celo_rpc_url = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
        self.w3 = Web3(Web3.HTTPProvider(self.celo_rpc_url))
        
        # Celo uses Proof-of-Authority (PoA) consensus, requiring this middleware
        self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        
        # Auto-derive the Hot Wallet from your 12 words!
        seed_phrase = os.getenv("CELO_MNEMONIC")
        if seed_phrase:
            account = Account.from_mnemonic(seed_phrase)
            self.treasury_private_key = account.key.hex()
            # For this test, we will just set the Exit address to the same wallet
            self.exit_address = account.address
        else:
            self.treasury_private_key = None
            self.exit_address = None

        # The official Native USDC Smart Contract Address on Celo Mainnet
        self.usdc_address = "0xcebA9300f2b948710d2653dD7B07f33A8B32118C"

    async def buy_telkom_airtime(self, usd_principal: float, discount_rate: float) -> float:
        """STATE 1: Procure wholesale airtime (Simulated for Celo testing)"""
        base_rate = 130.50
        kes_value = (usd_principal * base_rate) / (1 - discount_rate)
        
        if not self.live_mode:
            await asyncio.sleep(0.5)
            return kes_value
            
        return kes_value

    async def liquidate_to_fiat(self, airtime_kes_value: float) -> float:
        """STATE 2: Liquidate airtime to float (Simulated for Celo testing)"""
        if not self.live_mode:
            await asyncio.sleep(0.5)
            return airtime_kes_value
        return airtime_kes_value

    def _sync_celo_transfer(self, amount_usd: float) -> str:
        """
        Synchronous function that builds, signs, and sends the raw Ethereum/Celo transaction.
        """
        print(f"\n🌐 WEB3 ENGINE: Initiating on-chain settlement of {amount_usd} USDC...")
        
        if not self.treasury_private_key or not self.exit_address:
            raise ValueError("Missing CELO_MNEMONIC in .env file.")

        if not self.w3.is_connected():
            raise ConnectionError("Failed to connect to the Celo Blockchain RPC.")

        # 1. Load the Hot Wallet Account
        account = self.w3.eth.account.from_key(self.treasury_private_key)
        print(f"   ↳ Treasury Wallet Loaded: {account.address}")

        # 2. Instantiate the USDC Smart Contract
        usdc_contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.usdc_address), 
            abi=ERC20_ABI
        )

        # 3. Format the amount (USDC has 6 decimal places, not 18!)
        amount_base_units = int(amount_usd * 1_000_000)

        # 4. Get the latest Nonce (Transaction count) to prevent replay attacks
        nonce = self.w3.eth.get_transaction_count(account.address)

        print(f"   ↳ Building Smart Contract Payload...")
        
        # 5. Build the Transaction Dictionary
        tx = usdc_contract.functions.transfer(
            self.w3.to_checksum_address(self.exit_address),
            amount_base_units
        ).build_transaction({
            'chainId': 42220, # 42220 is the official Celo Mainnet Chain ID
            'gas': 150000,    # Standard gas limit for an ERC-20 transfer
            'gasPrice': self.w3.eth.gas_price,
            'nonce': nonce,
        })

        print(f"   ↳ Cryptographically Signing Transaction...")
        
        # 6. Sign the Transaction offline using your Private Key
        signed_tx = self.w3.eth.account.sign_transaction(tx, private_key=self.treasury_private_key)

        print(f"   ↳ Broadcasting to Celo Network...")
        
        # 7. Broadcast the raw hex to the global network!
        # Web3 v7 changed rawTransaction to raw_transaction
        raw_tx = getattr(signed_tx, 'raw_transaction', getattr(signed_tx, 'rawTransaction', None))
        tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
        
        hex_hash = self.w3.to_hex(tx_hash)
        print(f"   ↳ ✅ ON-CHAIN SUCCESS! TxHash: {hex_hash}\n")
        
        return hex_hash

    async def execute_celo_dex_swap(self, usda_amount: float) -> str:
        """
        STATE 5: Exits the corridor by moving real USDC on the Celo Blockchain.
        We run this in asyncio.to_thread so the heavy cryptographic signing 
        does not freeze the FastAPI web server for other users!
        """
        try:
            # Pushes the synchronous Web3 tasks to a background worker thread
            tx_hash = await asyncio.to_thread(self._sync_celo_transfer, usda_amount)
            return tx_hash
        except Exception as e:
            # We catch the exact blockchain error (e.g., "insufficient funds for gas")
            error_msg = str(e)
            print(f"\n   ↳ ❌ CELO BLOCKCHAIN REJECTED TRANSACTION: {error_msg}\n")
            raise Exception(f"Web3 Error: {error_msg}")

# Singleton export
corridor_api = CorridorIntegrations()