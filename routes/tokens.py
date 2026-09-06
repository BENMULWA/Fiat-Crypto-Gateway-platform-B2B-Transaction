from fastapi import APIRouter, Depends
from auth import get_current_user

router = APIRouter(prefix="/api/tokens", tags=["tokens"])

# Shared workspace token pool (mock)
WORKSPACE_TOKEN_POOL = {
    "total": 500000,
    "used": 120000,
    "remaining": 380000,
    "members": [
        {"userId": "admin", "email": "bernard@mamlakapsp.com", "allocated": 200000, "used": 80000},
        {"userId": "member1", "email": "mutukumjm@gmail.com", "allocated": 100000, "used": 40000},
    ],
}

MEMBER_TOKEN_DATA = {
    "total": 100000,
    "used": 40000,
    "remaining": 60000,
}


@router.get("/balance")
async def get_token_balance(as_admin: bool = False, current_user=Depends(get_current_user)):
    is_admin = current_user.get("role") == "admin"

    if is_admin or as_admin:
        return {
            "view": "admin",
            "pool": WORKSPACE_TOKEN_POOL,
        }
    else:
        return {
            "view": "member",
            "balance": MEMBER_TOKEN_DATA,
        }
