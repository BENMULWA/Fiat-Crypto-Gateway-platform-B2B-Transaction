import base64
import traceback
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# 1. Inputs exactly as extracted from your secure variables profile
ENCRYPTED_MASTER_KEY = b"gAAAAABqPVmWXei__YvkZfMzpqF53dII-n5Z30CEESjgLnXi7q6JjA_Sy8NBD_rHeAL1UCiGJ_3JSZOEmUBcs2777dUjqh2JDUpM0mvs1Mt7nvTpFCWoRglITaXIZSGJfIdeGCfzO6xo1hvy3_Ql8zg-MoNdxYIlrZkYU-sRlXl85QhsYueY7sQ="
WALLET_SALT = b"Jl++zt5LfZ/NX3yKYWGkQw=="
MAMLAKA_MASTER_PASSWORD = b"@BernardMam-laka2026Secured."

try:
    print("⏳ Deriving key matrix via PBKDF2 (480,000 iterations)...")
    
    # 2. Decode the raw base64 salt structure cleanly
    decoded_salt = base64.b64decode(WALLET_SALT)
    
    # 3. Setup the key derivation function using your high-security standards
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=decoded_salt,
        iterations=480_000,  # High iteration count for strong key stretching
    )
    
    # 4. Generate the Fernet-compatible key wrapper
    derived_key = base64.urlsafe_b64encode(kdf.derive(MAMLAKA_MASTER_PASSWORD))
    fernet = Fernet(derived_key)
    
    print("🔓 Extracting private key signatures...")
    decrypted_bytes = fernet.decrypt(ENCRYPTED_MASTER_KEY)
    
    # 5. Output the decrypted key context string
    print("\n🎉 SUCCESS! Decrypted Cardano Key Hex:")
    print("----------------------------------------------------------------")
    print(decrypted_bytes.decode('utf-8'))
    print("----------------------------------------------------------------")

except InvalidToken:
    print("\n❌ Cryptographic Decryption Failed: Invalid Token.")
    print("The derived key failed verification. Check string integrity values.")
except Exception as e:
    print("\n❌ Execution Interrupted:")
    traceback.print_exc()
