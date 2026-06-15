from motor.motor_asyncio import AsyncIOMotorClient
from config import settings

_client: AsyncIOMotorClient | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.mongo_url)
    return _client


def get_db():
    return get_client()[settings.mongo_db]


# --- Add your collection helpers below ---
def get_users_col():
    return get_db()["users"]


def get_teams_col():
    return get_db()["teams"]


def get_tokens_col():
    return get_db()["tokens"]


def get_transactions_col():
    return get_db()["transactions"]
