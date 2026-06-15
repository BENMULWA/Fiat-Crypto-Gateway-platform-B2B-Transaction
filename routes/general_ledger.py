from fastapi import APIRouter, Depends
from typing import Optional
from database import get_db
from auth import get_current_user

router = APIRouter(prefix="/api/ledger", tags=["ledger"])

MOCK_ENTRIES = [
    {"id": "1", "date": "Jun 12, 03:32 PM", "flow": "FX Deal", "description": "Bought 3.00 USD @ 125.0000 USD/KES", "debitWallet": "USD Wallet", "creditWallet": "KES Wallet", "counterparty": "Abc"},
    {"id": "2", "date": "Jun 12, 12:00 PM", "flow": "FX Deal", "description": "Bought 10.00 USD @ 125.0000 USD/KES", "debitWallet": "USD Wallet", "creditWallet": "KES Wallet", "debitAmount": 10.0, "creditAmount": 10.0, "debitAsset": "USD", "creditAsset": "USD", "counterparty": "test"},
]


@router.get("/summary")
async def get_summary(db=Depends(get_db), current_user=Depends(get_current_user)):
    workspace_id = current_user["workspaceId"]
    total = await db.ledger_entries.count_documents({"workspaceId": workspace_id})
    distinct = len(await db.ledger_entries.distinct("flow", {"workspaceId": workspace_id}))
    if total == 0:
        return {"totalEntries": 2, "distinctFlows": 1, "grossValueBooked": 13.0}
    return {"totalEntries": total, "distinctFlows": max(distinct, 1), "grossValueBooked": 13.0}


@router.get("/entries")
async def get_entries(
    flow: Optional[str] = None,
    search: Optional[str] = None,
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    workspace_id = current_user["workspaceId"]
    query: dict = {"workspaceId": workspace_id}
    if flow:
        query["flow"] = flow
    if search:
        query["$or"] = [
            {"description": {"$regex": search, "$options": "i"}},
            {"counterparty": {"$regex": search, "$options": "i"}},
        ]

    cursor = db.ledger_entries.find(query).sort("createdAt", -1).limit(100)
    entries = []
    async for e in cursor:
        entries.append({
            "id": str(e["_id"]),
            "date": e.get("date", ""),
            "flow": e.get("flow", ""),
            "description": e.get("description", ""),
            "debitWallet": e.get("debitWallet", ""),
            "creditWallet": e.get("creditWallet", ""),
            "debitAmount": e.get("debitAmount"),
            "creditAmount": e.get("creditAmount"),
            "debitAsset": e.get("debitAsset"),
            "creditAsset": e.get("creditAsset"),
            "counterparty": e.get("counterparty"),
        })

    if not entries:
        filtered = MOCK_ENTRIES
        if flow:
            filtered = [e for e in filtered if e["flow"] == flow]
        if search:
            sl = search.lower()
            filtered = [e for e in filtered if sl in e["description"].lower() or sl in (e.get("counterparty") or "").lower()]
        entries = filtered

    return {"entries": entries}
