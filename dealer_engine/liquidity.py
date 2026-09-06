from typing import Any


class LiquidityEngine:
    """Loads executable internal and external liquidity sources for an RFQ."""

    def __init__(self, db):
        self.db = db

    async def sources(self, asset: str, market_rate: float) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        position = await self.db["treasury_positions"].find_one({"asset": asset})
        if position:
            total = float(position.get("total", 0) or 0)
            reserved = float(position.get("reserved", 0) or 0)
            pending = float(position.get("pending", 0) or 0)
            available = max(total - reserved - pending, 0)
            sources.append({
                "id": "internal_treasury",
                "name": "Internal Treasury",
                "asset": asset,
                "rate": float(position.get("rate", market_rate) or market_rate),
                "fee": float(position.get("fee", 0) or 0),
                "available": available,
                "settlement_method": position.get("settlement_method", "internal"),
            })

        external = await self.db["liquidity_sources"].find({"asset": asset, "enabled": {"$ne": False}}).to_list(length=50)
        for source in external:
            available = float(source.get("available", 0) or 0)
            if available <= 0:
                continue
            sources.append({
                "id": str(source.get("id") or source.get("name")),
                "name": source.get("name", "External Liquidity"),
                "asset": asset,
                "rate": float(source.get("rate", market_rate) or market_rate),
                "fee": float(source.get("fee", 0) or 0),
                "available": available,
                "settlement_method": source.get("settlement_method", "provider"),
                "provider_quote_id": source.get("provider_quote_id"),
            })
        return sources


class SmartRouter:
    """Selects the cheapest executable route and splits across sources when needed."""

    def route(self, sources: list[dict[str, Any]], required: float) -> dict[str, Any]:
        required = float(required or 0)
        ranked = sorted(sources, key=lambda source: (float(source.get("rate", 0)) + float(source.get("fee", 0)), source.get("name", "")))
        allocations = []
        remaining = max(required, 0)
        for source in ranked:
            allocation = min(float(source.get("available", 0) or 0), remaining)
            if allocation <= 0:
                continue
            allocations.append({
                "sourceId": source["id"],
                "source": source["name"],
                "asset": source.get("asset"),
                "amount": round(allocation, 8),
                "rate": source["rate"],
                "fee": source["fee"],
                "settlementMethod": source.get("settlement_method"),
                "providerQuoteId": source.get("provider_quote_id"),
            })
            remaining -= allocation
            if remaining <= 0:
                break
        total = sum(item["amount"] for item in allocations)
        return {
            "required": required,
            "allocations": allocations,
            "totalAllocated": round(total, 8),
            "sufficient": total >= required,
            "shortfall": round(max(required - total, 0), 8),
            "blendedRate": round(sum(item["amount"] * item["rate"] for item in allocations) / total, 8) if total else 0,
        }
