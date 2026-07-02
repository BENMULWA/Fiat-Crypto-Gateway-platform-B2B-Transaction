"""
Run this file manually from your terminal when you want to simulate 
a customer depositing USDA from their external wallet into your platform.

Usage: python simulate_customer_deposit.py
"""
import os
from dotenv import load_dotenv
from pycardano import (
    PaymentSigningKey, 
    PaymentVerificationKey, 
    Address, 
    Network, 
    TransactionBuilder, 
    TransactionOutput, 
    BlockFrostChainContext,
    MultiAsset,
    Value
)

# Load environment variables
load_dotenv()
PROJECT_ID = os.getenv("BLOCKFROST_PROJECT_ID")
USDA_POLICY_ID = os.getenv("USDA_POLICY_ID")

def send_usda_to_platform():
    print("🔄 Connecting to Cardano Preprod...")
    context = BlockFrostChainContext(project_id=PROJECT_ID, base_url="https://cardano-preprod.blockfrost.io/api")

    # 1. Your Generated External Wallet (The Customer)
    cbor_hex = "5820e985b1e102d747c2135d50461a696289874dd1324386f922fd61dba792fd4b4c"
    sk = PaymentSigningKey.from_cbor(cbor_hex)
    vk = PaymentVerificationKey.from_signing_key(sk)
    customer_address = Address(payment_part=vk.hash(), network=Network.TESTNET)
    print(f"✅ Unlocked Customer Wallet: {customer_address}")

    # 2. PASTE THE DEPOSIT ADDRESS FROM YOUR REACT UI HERE
    DESTINATION_HOT_WALLET = "addr_test1vqdn6tcwslhsyyr9qhvh7lyfqzy4dgcn4ra8049hujry3fgd8ynj7"
    destination_address = Address.from_primitive(DESTINATION_HOT_WALLET)

    # 3. Build the Transaction
    print("💸 Building transaction to send USDA back to Platform...")
    builder = TransactionBuilder(context)
    builder.add_input_address(customer_address)

    # FIX 1: Convert 0.04 USDA to Lovelace units 
    # (Since 40,000 units is all this wallet currently has!)
    amount_usda = 0.04
    lovelace_usda = int(amount_usda * 1_000_000)

    # Construct the Custom Token payload
    my_asset = MultiAsset.from_primitive({
        bytes.fromhex(USDA_POLICY_ID): {
            b"USDA": lovelace_usda
        }
    })

    # FIX 2: Lower output ADA to 1.5 (1500000 lovelace)
    # This leaves 0.5 ADA behind to safely cover the network gas fee!
    builder.add_output(TransactionOutput(
        destination_address,
        Value(coin=1500000, multi_asset=my_asset)
    ))

    # 4. Sign and Submit
    signed_tx = builder.build_and_sign([sk], change_address=customer_address)
    context.submit_tx(signed_tx.to_cbor())

    print("\n🎉 DEPOSIT TRANSACTION SUBMITTED SUCCESSFULLY!")
    print("=" * 60)
    print(f"Transaction Hash: {signed_tx.id}")
    print("=" * 60)
    print("\nNext Steps:")
    print("1. Wait ~30 seconds for the block to mine.")
    print("2. Copy the Transaction Hash above.")
    print("3. Paste it into your React UI 'Paste TX Hash' box and click 'Confirm Deposit'!")

if __name__ == "__main__":
    send_usda_to_platform()