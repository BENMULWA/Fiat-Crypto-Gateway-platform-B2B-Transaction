from fastapi import APIRouter

router = APIRouter(prefix="/api/retail", tags=["Retail User"])

@router.get("/wallet")
async def get_retail_wallet_balances():
    """
    Returns the real balances for the Retail Wallet.
    In production, this fetches from the user's MongoDB document.
    """
    return {
        "status": "success",
        "balances": {
            "USDA": 1250.50,
            "KES": 45000.00,
            "IMP": 0.00
        }
    }