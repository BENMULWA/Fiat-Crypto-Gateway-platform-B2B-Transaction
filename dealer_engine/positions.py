from typing import Any

# The TreasuryPositionEngine class is responsible for managing and querying the available treasury inventory. 
# It provides methods to check the available balance of a specific asset, reserve a certain amount of that asset, and ensure that the requested amount does not exceed the available balance. The class interacts with the database to retrieve and update treasury positions, ensuring that customer balances are not mixed with treasury inventory.
class TreasuryPositionEngine:
    """Reads available treasury inventory without mixing it with customer balances."""

    def __init__(self, db):
        self.db = db

    async def available(self, asset: str) -> dict[str, float | str]:
        asset = str(asset or "").upper()
        position = await self.db["treasury_positions"].find_one({"asset": asset})
        if position:
            total = float(position.get("total", position.get("available", 0)) or 0)
            reserved = float(position.get("reserved", 0) or 0)
            pending = float(position.get("pending", 0) or 0)
            return {
                "asset": asset,
                "total": total,
                "reserved": reserved,
                "pending": pending,
                "available": max(total - reserved - pending, 0),
                "source": position.get("source", "treasury_positions"),
            }

        wallets = await self.db["retail_wallets"].find({}).to_list(length=None)
        total = sum(float(wallet.get(asset, 0) or 0) for wallet in wallets)
        return {
            "asset": asset,
            "total": total,
            "reserved": 0.0,
            "pending": 0.0,
            "available": total,
            "source": "retail_wallets_fallback",
        }

    async def reserve(self, asset: str, amount: float, reference: str) -> bool:
        amount = float(amount or 0)
        if amount <= 0:
            return False
        position = await self.available(asset)
        if float(position["available"]) < amount:
            return False
        result = await self.db["treasury_positions"].update_one(
            {
                "asset": str(asset).upper(),
                "$expr": {
                    "$gte": [
                        {"$subtract": [{"$subtract": ["$total", {"$ifNull": ["$reserved", 0]}]}, {"$ifNull": ["$pending", 0]}]},
                        amount,
                    ]
                },
            },
            {"$inc": {"reserved": amount}, "$push": {"reservations": {"reference": reference, "amount": amount}}},
            upsert=False,
        )
        modified_count = getattr(result, "modified_count", None)
        if modified_count is None and isinstance(result, dict):
            modified_count = result.get("modified_count", 0)
        return bool(modified_count)

    async def reserve_route(self, allocations: list[dict[str, Any]], reference: str) -> bool:
        """Reserve every internal allocation, rolling back earlier reservations on failure."""
        reserved: list[tuple[str, float]] = []
        for allocation in allocations:
            if allocation.get("settlementMethod") != "internal":
                continue
            asset = str(allocation.get("asset") or "").upper()
            amount = float(allocation.get("amount", 0) or 0)
            if not asset or not await self.reserve(asset, amount, reference):
                for rollback_asset, rollback_amount in reserved:
                    await self.db["treasury_positions"].update_one(
                        {"asset": rollback_asset},
                        {"$inc": {"reserved": -rollback_amount}},
                    )
                return False
            reserved.append((asset, amount))
        return True
