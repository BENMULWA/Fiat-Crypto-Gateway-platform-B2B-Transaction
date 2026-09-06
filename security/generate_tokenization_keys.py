import os
from pathlib import Path

from pycardano import PaymentSigningKey, PaymentVerificationKey, ScriptPubkey


KEY_FILE = Path(__file__).resolve().parent / "airt-policy-signing-key.hex"

def generate_airt_policy():
    print("========================================")
    print("🔐 MAMLAKA AIRT ISOLATED KEY GENERATOR")
    print("========================================")

    # 1. Generate a brand new, isolated signing key
    sk = PaymentSigningKey.generate()
    sk_hex = sk.to_cbor().hex()
    
    # 2. Derive the verification key
    vk = PaymentVerificationKey.from_signing_key(sk)
    
    # 3. Build the Native Script Policy & Get ID
    key_hash = vk.hash()
    policy = ScriptPubkey(key_hash)
    policy_id = policy.hash().payload.hex()
    
    print("✅ AIRT Keys Generated Successfully!\n")
    KEY_FILE.write_text(sk_hex + "\n")
    os.chmod(KEY_FILE, 0o600)
    print("The signing key was saved locally with mode 0600.")
    print(f"Signing key file: {KEY_FILE}")
    print(f"CARDANO_AIRT_POLICY_ID={policy_id}")
    print(f"CARDANO_AIRT_ASSET_NAME=AIRT")
    print(f"CARDANO_AIRT_DECIMALS=0")
    print("\n========================================")

if __name__ == "__main__":
    generate_airt_policy()