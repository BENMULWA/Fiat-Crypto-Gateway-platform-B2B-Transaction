import os
import asyncio
import json
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from dotenv import load_dotenv
from eth_account import Account


# load environment variables from .env file
load_dotenv()
Account.enable_unaudited_hdwallet_features()

# Minimal ABI required to execute an ERC-20 Token Transfer on the blockchain
ERC20_ABI = json.loads('[{"constant":false,"inputs":[{"name":"_to","type":"address"},{"name":"_value","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"type":"function"}]')

class CorridorIntegrations:
    def __init__(self):
        
        # bot live mode is switched off by default for testing and development
        self.live_mode = False
        self.celo_rpc_url = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
        
        # =========================================================
        # 🚀 AWS PRODUCTION UPGRADE: The Bulletproof Web3 Provider
        # =========================================================
        # 1. Create a persistent session
        session = requests.Session()
        
        # 2. Configure an aggressive Auto-Retry strategy for Cloud environments
        retry_strategy = Retry(
            total=5,  # Try up to 5 times if the network drops
            backoff_factor=0.5,  # Wait 0.5s, then 1s, then 2s between retries
            status_forcelist=[429, 500, 502, 503, 504], # Retry on these specific server errors
            allowed_methods=["POST", "GET", "OPTIONS"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        # 3. Inject the Session and a 60-second timeout into Web3
        self.w3 = Web3(Web3.HTTPProvider(
            self.celo_rpc_url,
            session=session,
            request_kwargs={'timeout': 60} # Force AWS to wait up to 60 seconds for blockchain confirmation
        ))
        
        # Celo uses Proof-of-Authority (PoA) consensus, requiring this middleware to handle the extra data in blocks
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)        
        # Auto-derive the Hot Wallet from your 12 words!
        # Auto-derive the Hot Wallet from your 12 words!
        seed_phrase = os.getenv("CELO_MNEMONIC")
        if seed_phrase:
            account = Account.from_mnemonic(seed_phrase)
            self.treasury_private_key = account.key.hex()
            
            # MAKE SURE IT SAYS THIS 
            self.exit_address = os.getenv("CELO_EXIT_ADDRESS")
        else:
            self.treasury_private_key = None
            self.exit_address = None

        # The official Native USDC Smart Contract Address on Celo Mainnet
        self.usdc_address = "0xcebA9300f2b948710d2653dD7B07f33A8B32118C"

    def _sync_celo_transfer(self, amount_usd: float, token_address: str | None = None,
                             to_address: str | None = None, decimals: int = 6) -> str:
        """
        Synchronous function that builds, signs, and sends the raw Ethereum/Celo transaction.
        Generalized beyond USDC-only: token_address/to_address/decimals let
        transfer_erc20() below reuse this exact signing/gas/broadcast logic
        for any real ERC-20 (e.g. depositing minted IMC into a Comet-
        managed wallet before a swap) instead of duplicating it.
        """
        token_address = token_address or self.usdc_address
        to_address = to_address or self.exit_address
        print(f"\n🌐 WEB3 ENGINE: Initiating on-chain settlement of {amount_usd} tokens...")

        if not self.treasury_private_key or not to_address:
            raise ValueError("Missing CELO_MNEMONIC or a destination address.")

        if not self.w3.is_connected():
            raise ConnectionError("Failed to connect to the Celo Blockchain RPC.")

        # 1. Load the Hot Wallet Account
        account = self.w3.eth.account.from_key(self.treasury_private_key)
        print(f"   ↳ Treasury Wallet Loaded: {account.address}")
        print(f"   ↳ Target: {to_address}")

        # 2. Instantiate the ERC-20 contract
        token_contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(token_address),
            abi=ERC20_ABI
        )

        # 3. Format the amount at the token's real decimals (USDC/USDT/IMC
        # are 6; caller passes a different `decimals` for an 18-decimal
        # token like cUSD).
        amount_base_units = int(amount_usd * (10 ** decimals))

        # 4. Get the latest Nonce (Transaction count) to prevent replay attacks
        nonce = self.w3.eth.get_transaction_count(account.address)

        print(f"   ↳ Building Smart Contract Payload...")

        # 5. Estimate the contract gas instead of reserving an unnecessarily large fixed limit.
        transfer_call = token_contract.functions.transfer(
            self.w3.to_checksum_address(to_address),
            amount_base_units
        )
        transaction_base = {
            'chainId': 42220, # 42220 is the official Celo Mainnet Chain ID
            'gasPrice': self.w3.eth.gas_price,
            'nonce': nonce,
            'from': account.address,
        }
        estimated_gas = self.w3.eth.estimate_gas(
            transfer_call.build_transaction(transaction_base)
        )
        gas_limit = (estimated_gas * 120 + 99) // 100
        required_gas_wei = gas_limit * transaction_base['gasPrice']
        native_balance_wei = self.w3.eth.get_balance(account.address)
        if native_balance_wei < required_gas_wei:
            shortfall_wei = required_gas_wei - native_balance_wei
            raise ValueError(
                "Insufficient native CELO for gas: "
                f"need {self.w3.from_wei(required_gas_wei, 'ether')} CELO, "
                f"have {self.w3.from_wei(native_balance_wei, 'ether')} CELO, "
                f"short by {self.w3.from_wei(shortfall_wei, 'ether')} CELO"
            )

        tx = transfer_call.build_transaction({
            **transaction_base,
            'gas': gas_limit,
        })

        print(f"   ↳ Cryptographically Signing Transaction...")
        
        # 6. Sign the Transaction offline using your Private Key
        signed_tx = self.w3.eth.account.sign_transaction(tx, private_key=self.treasury_private_key)

        print(f"   ↳ Broadcasting to Celo Network...")
        
        # 7. Broadcast the raw hex to the global network!
        raw_tx = getattr(signed_tx, 'raw_transaction', getattr(signed_tx, 'rawTransaction', None))
        tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
        
        hex_hash = self.w3.to_hex(tx_hash)
        print(f"   ↳ ✅ ON-CHAIN SUCCESS! TxHash: {hex_hash}\n")
        
        return hex_hash

    async def transfer_erc20(self, token_address: str, to_address: str, amount: float, decimals: int = 6) -> str:
        """Real signed ERC-20 transfer from the treasury's self-custodied
        wallet to any address, on any token. Added so _execute_mint_comet
        can deposit freshly-minted IMC into the Comet-managed wallet
        (from comet_client.get_or_create_wallet) before calling
        execute_imm_swap — Comet's tokenize_airtime mints into the caller's
        own treasury wallet, not into Comet's custodial wallet, so the
        corridor must move the funds itself first. This is a real broadcast
        transaction; callers must have real CELO for gas and treat failures
        (e.g. insufficient gas) as a hard stop, not a retry-forever loop."""
        try:
            return await asyncio.to_thread(
                self._sync_celo_transfer, amount, token_address, to_address, decimals
            )
        except Exception as e:
            error_msg = str(e)
            print(f"\n   ↳ ❌ CELO BLOCKCHAIN REJECTED TRANSACTION: {error_msg}\n")
            raise Exception(f"Web3 Error: {error_msg}")

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