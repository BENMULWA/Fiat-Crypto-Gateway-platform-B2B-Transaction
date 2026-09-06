from fastapi import APIRouter, Depends
from auth import get_current_user

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

MOCK_DASHBOARD = {
    "portfolioValue": 92.68,
    "airtHeld": -88.0,
    "usdaHeld": 191.0,
    "openOrders": 1,
    "balances": [
        {"asset": "USDA", "label": "USDA", "amount": 191.0, "usdEquivalent": 191.0},
        {"asset": "USD", "label": "USD", "amount": 3.0, "usdEquivalent": 3.0},
        {"asset": "KES", "label": "KES", "amount": -1718.0, "usdEquivalent": -13.32},
        {"asset": "AIRT", "label": "AIRT", "amount": -88.0, "usdEquivalent": -88.0},
    ],
    "recentRampActivity": [
        {"id": "1", "fromAmount": 3.0, "toAmount": 3.0, "fromAsset": "KES", "toAsset": "USDA", "status": "completed", "timeAgo": "2d ago"},
        {"id": "2", "fromAmount": 10.0, "toAmount": 10.0, "fromAsset": "USD", "toAsset": "KES", "status": "completed", "timeAgo": "3d ago"},
        {"id": "3", "fromAmount": 100.0, "toAmount": 100.0, "fromAsset": "KES", "toAsset": "USDA", "status": "completed", "timeAgo": "4d ago"},
    ],
    "airtimePeg": {"collateralization": -78.6, "airtMinted": 112.0, "usdaReserve": -88.0},
}


@router.get("")
async def get_dashboard(current_user=Depends(get_current_user)):
    return MOCK_DASHBOARD
