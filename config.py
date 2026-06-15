import os
from typing import Optional
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

# Force Python to find and load the .env file in the current folder
load_dotenv()


class Settings(BaseSettings):
    # It will look into your .env first. If missing, it uses these defaults:
    mongo_url: str = os.getenv("MONGO_URL", "mongodb://localhost:27017")
    mongo_db: str = os.getenv("MONGO_DB", "meshex")
    jwt_secret: str = os.getenv("JWT_SECRET", "changeme-super-secret-jwt-key-32chars")
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "HS256")
    jwt_expiry_hours: int = int(os.getenv("JWT_EXPIRY_HOURS", "24"))
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # Cardano / Blockfrost
    blockfrost_project_id: Optional[str] = os.getenv("BLOCKFROST_PROJECT_ID", None)
    # "mainnet" | "preprod" | "preview"
    cardano_network: str = os.getenv("CARDANO_NETWORK", "preprod")
    # BIP39 24-word mnemonic for the platform hot wallet
    cardano_mnemonic: Optional[str] = os.getenv("CARDANO_MNEMONIC", None)
    # Anzens USDA on Cardano — override per network if needed
    usda_policy_id: str = os.getenv(
        "USDA_POLICY_ID", "f43a62fdc3965df486de8a0d32fe800963589c4094f547c4b8b3e40"
    )
    usda_asset_name_hex: str = os.getenv("USDA_ASSET_NAME_HEX", "55534441")  # ASCII "USDA"
    usda_decimals: int = int(os.getenv("USDA_DECIMALS", "6"))
    # Minimum ADA to attach when sending native tokens (lovelace)
    cardano_min_utxo_lovelace: int = int(os.getenv("CARDANO_MIN_UTXO_LOVELACE", "2000000"))

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
