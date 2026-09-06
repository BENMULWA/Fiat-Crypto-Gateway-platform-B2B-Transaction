import os
from typing import Optional
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# Always load backend/.env regardless of process working directory.
load_dotenv(dotenv_path=ENV_PATH)

class Settings(BaseSettings):
    # Base Configuration
    mongo_url: str = os.getenv("MONGO_URL", "mongodb://localhost:27017")
    mongo_db: str = os.getenv("MONGO_DB", "meshex")
    jwt_secret: str = os.getenv("JWT_SECRET", "changeme-super-secret-jwt-key-32chars")
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "HS256")
    jwt_expiry_hours: int = int(os.getenv("JWT_EXPIRY_HOURS", "24"))
    
    # ── FIX: Added to prevent route module AttributeErrors ──────────────────
    secret_key: str = os.getenv("SECRET_KEY", "changeme-super-secret-jwt-key-32chars")

    # Email OTP / SMTP
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from_email: str = os.getenv("SMTP_FROM_EMAIL", "")
    smtp_use_tls: bool = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
    admin_alert_emails: str = os.getenv("ADMIN_ALERT_EMAILS", "")

    otp_expiry_minutes: int = int(os.getenv("OTP_EXPIRY_MINUTES", "10"))
    otp_length: int = int(os.getenv("OTP_LENGTH", "6"))
    otp_max_attempts: int = int(os.getenv("OTP_MAX_ATTEMPTS", "5"))
    otp_resend_cooldown_seconds: int = int(os.getenv("OTP_RESEND_COOLDOWN_SECONDS", "30"))
    otp_max_resends: int = int(os.getenv("OTP_MAX_RESENDS", "5"))

    # Used to build the password-reset link emailed to users — must point at
    # the deployed frontend, not the API, since /reset-password?token=... is
    # a React route (see src/pages/ResetPasswordPage.tsx).
    frontend_url: str = os.getenv("FRONTEND_URL", "http://localhost:5173")
    password_reset_expiry_minutes: int = int(os.getenv("PASSWORD_RESET_EXPIRY_MINUTES", "30"))
    
    # Comma-separated allowlist. Never use "*" with credentialed requests.
    # This must stay a plain str field, not list[str] — pydantic-settings
    # intercepts any env var matching a field name and, for list-typed
    # fields, tries to JSON-decode it rather than using this class's own
    # comma-split logic. A previous version of this field was list[str] and
    # crashed the app at startup the moment CORS_ORIGINS was actually set in
    # the environment (as opposed to left to the Python-side default below,
    # which pydantic-settings never touches since there's no env var to
    # intercept). The field_validator below does the comma-splitting instead,
    # after pydantic-settings has already handed off a plain string.
    cors_origins_raw: str = os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,https://jasiricapital.africa,https://fiat-platform-fronted-git-feature-your-659257-ray-gees-projects.vercel.app",
    )

    @property
    def cors_origins(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.cors_origins_raw.split(",")
            if origin.strip() and origin.strip() != "*"
        ]

    # Cardano / Blockfrost
    blockfrost_project_id: Optional[str] = os.getenv("BLOCKFROST_PROJECT_ID", None)
    cardano_network: str = os.getenv("CARDANO_NETWORK", "preprod")
    cardano_mnemonic: Optional[str] = os.getenv("CARDANO_MNEMONIC", None)
    usda_policy_id: str = os.getenv("USDA_POLICY_ID", "f43a62fdc3965df486de8a0d32fe800963589c4094f547c4b8b3e40")
    usda_asset_name_hex: str = os.getenv("USDA_ASSET_NAME_HEX", "55534441")  # ASCII "USDA"
    usda_decimals: int = int(os.getenv("USDA_DECIMALS", "6"))
    cardano_min_utxo_lovelace: int = int(os.getenv("CARDANO_MIN_UTXO_LOVELACE", "2000000"))
    # Optional: platform-wide account index used for hot wallet operations (top-ups)
    cardano_platform_account_index: int = int(os.getenv("CARDANO_PLATFORM_ACCOUNT_INDEX", "0"))
    # Optional ADA->USD conversion rate for UI display (set in .env for approximate USD values)
    cardano_ada_usd_rate: Optional[float] = None if os.getenv("CARDANO_ADA_USD_RATE") is None else float(os.getenv("CARDANO_ADA_USD_RATE"))

    # Pydantic v2 Environment parsing rules
    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
