# Excute the celo integration credentials script to generate the wallet address 
# and private key for the treasury wallet. This script reads the CELO_MNEMONIC from the .env file and derives the wallet credentials.

import os
from dotenv import load_dotenv
from eth_account import Account

# Force Python to read the .env file
load_dotenv()

# Get the mnemonic, but do NOT provide a fallback sentence
seed_phrase = os.getenv("CELO_MNEMONIC")

if not seed_phrase:
    raise ValueError("❌ ERROR: CELO_MNEMONIC not found in .env file! Please check your spelling.")

# Enable mnemonic features in web3
Account.enable_unaudited_hdwallet_features()

# Derive the wallet
account = Account.from_mnemonic(seed_phrase)

print("\n" + "="*50)
print("🔑 MAMLAKA WALLET CREDENTIALS 🔑")
print("="*50)
print(f"CELO_EXIT_ADDRESS = {account.address}")
print(f"CELO_TREASURY_PK  = {account.key.hex()}")
print("="*50 + "\n")