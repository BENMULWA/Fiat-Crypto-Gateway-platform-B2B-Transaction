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

from cardano.client import get_network #tels the structure to build a real money - connected to mainnet or dummy / fake money when connected to test net
from config import settings


class CardanoWallet:
    def __init__(self, account_index: int):
        if not settings.cardano_mnemonic:
            raise RuntimeError(
                "CARDANO_MNEMONIC is not set. "
                "Generate a 24-word mnemonic and add it to your .env file."
            )
        try:
            from pycardano import (
                HDWallet,
                PaymentSigningKey,
                PaymentVerificationKey,
                PaymentExtendedSigningKey,
                Address,
            )
        except ImportError:
            raise RuntimeError("pycardano is not installed. Run: pip install pycardano")

        root = HDWallet.from_mnemonic(settings.cardano_mnemonic)
        child = root.derive_from_path(f"m/1852'/1815'/{account_index}'/0/0")

        # Prefer creating an extended payment signing key from the HDWallet child
        # (this matches pycardano's expected ExtendedSigningKey payload layout).
        try:
            # If the HDWallet exposes extended private key material, build an
            # extended signing key (preferred).
            self._signing_key = PaymentExtendedSigningKey.from_hdwallet(child)
        except Exception:
            # Fallback for older pycardano versions: try to extract a 32-byte seed
            priv = None
            for attr in ("private_key", "xprivate_key", "sk", "to_xprv", "to_bytes"):
                if hasattr(child, attr):
                    priv = getattr(child, attr)
                    break

            if priv is None:
                raise RuntimeError("Unable to extract private key from HDWallet child; incompatible pycardano version")

            # Normalize to bytes
            if isinstance(priv, str):
                try:
                    priv_bytes = bytes.fromhex(priv)
                except Exception:
                    priv_bytes = priv.encode()
            elif isinstance(priv, int):
                priv_bytes = priv.to_bytes((priv.bit_length() + 7) // 8, "big")
            else:
                priv_bytes = priv

            # Ensure 32-byte seed for NaCl SigningKey.
            # Prefer left 32 bytes of extended private material if present.
            if len(priv_bytes) == 32:
                seed32 = priv_bytes
            elif len(priv_bytes) >= 64:
                # child.xprivate_key or similar may contain 64 bytes (kL||kR).
                # Use the left 32 bytes (kL) as the seed for ed25519 signing.
                seed32 = priv_bytes[:32]
            elif len(priv_bytes) > 32:
                seed32 = priv_bytes[:32]
            else:
                raise RuntimeError("Extracted private key is not long enough to derive a 32-byte seed")

            self._signing_key = PaymentSigningKey.from_primitive(seed32)
        # Derive verification key and address from the signing key.
        # Both extended and non-extended signing keys support `from_signing_key`.
        if hasattr(self._signing_key, "to_verification_key"):
            vk = PaymentVerificationKey.from_signing_key(self._signing_key)
        else:
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
