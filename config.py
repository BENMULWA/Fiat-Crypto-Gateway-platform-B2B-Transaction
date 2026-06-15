import os
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

# Force Python to find and load the .env file in the current folder
load_dotenv()

class Settings(BaseSettings):
    # Base Configuration
    mongo_url: str = os.getenv("MONGO_URL", "mongodb://localhost:27017")
    mongo_db: str = os.getenv("MONGO_DB", "meshex")
    jwt_secret: str = os.getenv("JWT_SECRET", "changeme-super-secret-jwt-key-32chars")
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "HS256")
    jwt_expiry_hours: int = int(os.getenv("JWT_EXPIRY_HOURS", "24"))
    
    # Crucial Fix: Read the list from .env, or use these live defaults
    cors_origins: list[str] = [
        "http://localhost:5173", 
        "http://127.0.0.1:5173", 
        "https://fiat-platform-fronted.vercel.app"
    ]

    # Cardano / Blockfrost
    blockfrost_project_id: Optional[str] = os.getenv("BLOCKFROST_PROJECT_ID", None)
    cardano_network: str = os.getenv("CARDANO_NETWORK", "preprod")
    cardano_mnemonic: Optional[str] = os.getenv("CARDANO_MNEMONIC", None)
    usda_policy_id: str = os.getenv("USDA_POLICY_ID", "f43a62fdc3965df486de8a0d32fe800963589c4094f547c4b8b3e40")
    usda_asset_name_hex: str = os.getenv("USDA_ASSET_NAME_HEX", "55534441")  # ASCII "USDA"
    usda_decimals: int = int(os.getenv("USDA_DECIMALS", "6"))
    cardano_min_utxo_lovelace: int = int(os.getenv("CARDANO_MIN_UTXO_LOVELACE", "2000000"))

    # Pydantic v2 Environment parsing rules
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
