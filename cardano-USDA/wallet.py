"""
This provides HD(Heirachical Deterministic) wallet derivation, adress generation, and signing of transactions.
PaymentSigningKey: This is the Private Key. It is used to digitally sign transactions to move money. It must stay a complete secret on your server.
PaymentVerificationKey: This is the Public Key. It is made directly from your private key. It acts like an ID badge that proves who you are without showing your secret private key.
The code uses path m/1852'/1815'/{account_index}'/0/0 to find exact branch asigned to each user.
- 1852': Means "This is a standard Cardano Shelley wallet.
- 1815': The official crypto code number for ADA coin.
- {account_index}': This changes for every user! User A gets branch 0', User B gets branch 1', User C gets branch 2', and so on.
"""
from __future__ import annotations
import os

from cardano.client import get_network #tels the structure to build a real money - connected to mainnet or dummy / fake money when connected to test net
from config import settings
from security.encryption import decrypt_private_key

class CardanoWallet:
    def __init__(self, account_index: int = 0):
        # 1. We ignore 'account_index' now because Mamlaka is Custodial.
        # Everyone uses the exact same Master Wallet!
        
        encrypted_key = os.getenv("ENCRYPTED_MASTER_KEY")
        salt = os.getenv("WALLET_SALT")
        password = os.getenv("MAMLAKA_MASTER_PASSWORD")

        if not encrypted_key or not salt or not password:
            raise RuntimeError(
                "Vault Error: Missing ENCRYPTED_MASTER_KEY, WALLET_SALT, or MAMLAKA_MASTER_PASSWORD in .env"
            )

        try:
            from pycardano import (
                PaymentSigningKey,
                PaymentVerificationKey,
                PaymentExtendedSigningKey,
                Address,
            )
        except ImportError:
            raise RuntimeError("pycardano is not installed. Run: pip install pycardano")

        # 2. Unlock the Master Vault in RAM
        try:
            raw_cbor = decrypt_private_key(encrypted_key, salt, password)
            
            # CLEANUP: Remove accidental quotes, commas, or spaces from copy-pasting
            clean_cbor = raw_cbor.replace('"', '').replace("'", "").replace(",", "").strip()
            
            # Attempt to load as a standard signing key
            try:
                self._signing_key = PaymentSigningKey.from_cbor(clean_cbor)
            except Exception:
                # Fallback: If your wallet was generated as an Extended key
                self._signing_key = PaymentExtendedSigningKey.from_cbor(clean_cbor)
                
        except Exception as e:
            raise RuntimeError(f"Security Halt: Failed to decrypt Master Wallet. Incorrect password? Error: {str(e)}")

        # 3. Derive the public address from the unlocked private key
        vk = PaymentVerificationKey.from_signing_key(self._signing_key)
        self.address: Address = Address(payment_part=vk.hash(), network=get_network())

    @property
    def address_str(self) -> str:
        return str(self.address)

    @property
    def signing_key(self):
        return self._signing_key


async def get_or_create_wallet_index(db, workspace_id: str) -> int:
    """
    Returns the account index for a workspace, creating one if it doesn't exist. Workspace is the Storage iD for each user
    Index is stored in the `cardano_wallets` collection.
    (Kept for database compatibility, but not used for key derivation anymore).
    """
    doc = await db.cardano_wallets.find_one({"workspaceId": workspace_id})
    if doc:
        return int(doc["accountIndex"])

    count = await db.cardano_wallets.count_documents({})
    new_index = count
    await db.cardano_wallets.insert_one(
        {"workspaceId": workspace_id, "accountIndex": new_index}
    )
    return new_index