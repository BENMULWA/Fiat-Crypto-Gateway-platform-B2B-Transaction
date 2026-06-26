import os
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

def generate_encryption_key(master_password: str, salt: bytes) -> bytes:
    """
    Stretches a master password into a secure 32-byte encryption key.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,
    )
    return base64.urlsafe_b64encode(kdf.derive(master_password.encode()))

def encrypt_private_key(cbor_hex: str, master_password: str) -> dict:
    """
    Encrypts the plain Cardano private key (cborHex) so it is safe to store.
    """
    salt = os.urandom(16)
    key = generate_encryption_key(master_password, salt)
    f = Fernet(key)
    
    encrypted_data = f.encrypt(cbor_hex.encode())
    
    return {
        "salt": base64.b64encode(salt).decode('utf-8'),
        "encrypted_key": encrypted_data.decode('utf-8')
    }

def decrypt_private_key(encrypted_key: str, salt_b64: str, master_password: str) -> str:
    """
    Used by your FastAPI app in memory to unlock the key right before signing a transaction.
    """
    salt = base64.b64decode(salt_b64)
    key = generate_encryption_key(master_password, salt)
    f = Fernet(key)
    
    return f.decrypt(encrypted_key.encode()).decode('utf-8')

if __name__ == "__main__":
    print("🔒 MAMLAKA SECURE WALLET ENCRYPTION 🔒")
    print("="*50)
    
    raw_cbor = input("Paste your raw Mainnet cborHex private key: ").strip()
    password = input("Create a strong Master Password to lock this key: ").strip()
    
    result = encrypt_private_key(raw_cbor, password)
    
    print("\n✅ ENCRYPTION SUCCESSFUL!")
    print("="*45)
    print("Add these to your production .env file:")
    print(f"ENCRYPTED_MASTER_KEY={result['encrypted_key']}")
    
    print(f"\n WALLET_SALT={result['salt']}")
    print("="*45)
    print("⚠️  Store your Master Password in a secure Password Manager.")
    print("Your FastAPI server will need this password injected at runtime (e.g., as an environment variable in AWS/Heroku/DigitalOcean).")