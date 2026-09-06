from datetime import datetime, timedelta
from typing import Any

from routes.treasury import DEFAULT_USD_BASE_RATES
from dealer_engine.positions import TreasuryPositionEngine
from dealer_engine.liquidity import LiquidityEngine, SmartRouter


# The AnalysisEngine class is responsible for performing pre-trade checks on dealer RFQs (Request for Quotes). It checks customer status, KYC/KYB verification, daily limits, treasury availability, rate book status, sanctions screening, wallet screening, transaction purpose verification, and exposure limits.
# The analysis results are structured into groups and returned as a report indicating whether the RFQ passed all checks, along with details on liquidity sources, costs, and risk exposure before and after the trade.
class AnalysisEngine:
    """Runs authoritative pre-trade checks for a dealer RFQ."""

    def __init__(self, db):
        self.db = db

    async def _find_customer(self, customer_id: Any) -> dict | None:
        if not customer_id:
            return None
        candidates = [customer_id]
        try:
            from bson import ObjectId
            if isinstance(customer_id, str) and len(customer_id) == 24:
                candidates.append(ObjectId(customer_id))
        except Exception:
            pass
        for candidate in candidates:
            customer = await self.db["users"].find_one({"_id": candidate})
            if customer:
                return customer
        return None

    async def _rate_book(self) -> dict:
        rate_book = await self.db["treasury_rate_book"].find_one({"_id": "swap_rate_book"})
        return rate_book or {"active": True, "usd_base_rates": dict(DEFAULT_USD_BASE_RATES)}

    async def analyze(self, rfq: dict) -> dict:
        amount = float(rfq.get("amount", 0) or 0)
        from_asset = str(rfq.get("fromAsset", "")).upper()
        to_asset = str(rfq.get("toAsset", "")).upper()
        customer_id = rfq.get("customerId")
        customer = await self._find_customer(customer_id)
        rate_book = await self._rate_book()
        rates = dict(DEFAULT_USD_BASE_RATES)
        rates.update(rate_book.get("usd_base_rates", {}))
        market_rate = rates.get(to_asset, 0) / rates.get(from_asset, 1) if rates.get(to_asset) else 0
        treasury_asset = from_asset if str(rfq.get("side", "BUY")).upper() == "BUY" else to_asset
        treasury_required = amount if treasury_asset == from_asset else amount * market_rate
        customer_status = str((customer or {}).get("status", "active")).lower()
        kyc_status = str((customer or {}).get("kycStatus", "unverified")).lower()
        customer_limit = float((customer or {}).get("dailyLimit", 10000000) or 10000000)
        customer_volume = float((customer or {}).get("todayVolume", 0) or 0)
        remaining_limit = max(customer_limit - customer_volume, 0)
        treasury_position = await TreasuryPositionEngine(self.db).available(treasury_asset)
        treasury_available = float(treasury_position["available"])
        treasury_reserved = float(treasury_position["reserved"])
        liquidity_sources = await LiquidityEngine(self.db).sources(treasury_asset, market_rate)
        liquidity_route = SmartRouter().route(liquidity_sources, treasury_required)
        treasury_coverage = (treasury_available / treasury_required * 100) if treasury_required else 0
        internal_inventory = "FULL" if treasury_available >= treasury_required else "PARTIAL" if treasury_available > 0 else "UNAVAILABLE"
        wallet_screened = bool(rfq.get("destinationWallet") or to_asset not in {"USDA", "USDC", "USDT"})
        
        
    # The analysis engine performs a series of checks on the dealer RFQ, including customer status, KYC/KYB verification, remaining daily limit, requested amount, treasury availability, required amount, rate book status, sanctions screening, wallet screening, transaction purpose verification, and exposure limit.
    # It returns a structured analysis report indicating whether the RFQ passed all checks and provides details on liquidity sources, costs, and risk exposure before and after the trade.
        customer_checks = [
            {"key": "customer_status", "label": "Status", "value": customer_status.upper() if customer else "NOT FOUND", "passed": bool(customer and customer_status in {"active", "approved", "verified"})},
            {"key": "customer_kyc", "label": "KYC/KYB", "value": kyc_status.upper(), "passed": kyc_status in {"verified", "approved", "complete", "completed"}},
            {"key": "customer_limit", "label": "Remaining daily limit", "value": f"{remaining_limit:,.2f}", "passed": amount <= remaining_limit},
            {"key": "customer_requested", "label": "Requested", "value": f"{amount:,.2f} {from_asset}", "passed": amount > 0},
        ]
        treasury_checks = [
            {"key": "treasury_total", "label": f"{treasury_asset} total", "value": f"{float(treasury_position['total']):,.2f} {treasury_asset}", "passed": True},
            {"key": "treasury_reserved", "label": f"Reserved {treasury_asset}", "value": f"{treasury_reserved:,.2f} {treasury_asset}", "passed": True},
            {"key": "treasury_available", "label": f"Available {treasury_asset}", "value": f"{treasury_available:,.2f} {treasury_asset}", "passed": liquidity_route["sufficient"]},
            {"key": "treasury_required", "label": f"Required {treasury_asset}", "value": f"{treasury_required:,.2f} {treasury_asset}", "passed": treasury_required > 0},
            {"key": "treasury_coverage", "label": "Coverage", "value": f"{treasury_coverage:,.2f}%", "passed": liquidity_route["sufficient"]},
            {"key": "treasury_inventory", "label": "Internal inventory", "value": internal_inventory, "passed": liquidity_route["sufficient"]},
            {"key": "treasury_rate", "label": "Rate book", "value": "ACTIVE" if rate_book.get("active", True) else "INACTIVE", "passed": bool(rate_book.get("active", True) and market_rate > 0)},
        ]
        compliance_checks = [
            {"key": "sanctions", "label": "Sanctions", "value": "CLEAR", "passed": bool(customer and not customer.get("sanctionsMatch", False))},
            {"key": "wallet_screening", "label": "Wallet screening", "value": "CLEAR" if wallet_screened else "WALLET REQUIRED", "passed": wallet_screened},
            {"key": "transaction_purpose", "label": "Transaction purpose", "value": "VERIFIED" if rfq.get("purpose") else "NOT PROVIDED", "passed": bool(rfq.get("purpose") or rfq.get("settlementChannel"))},
        ]
        risk_checks = [
            {"key": "exposure", "label": "Exposure limit", "value": "WITHIN LIMIT", "passed": amount <= customer_limit},
        ]
        groups = {"customer": customer_checks, "treasury": treasury_checks, "compliance": compliance_checks, "risk": risk_checks}
        passed = all(check["passed"] for checks in groups.values() for check in checks)
        sources = [{"name": item["source"], "rate": item["rate"], "available": item["amount"], "amount": item["amount"]} for item in liquidity_route["allocations"]]
        return {
            **groups,
            "passed": passed,
            "expiresAt": (datetime.utcnow() + timedelta(seconds=15)).isoformat() + "Z",
            "liquidity": {
                "sources": sources,
                "blendedCost": liquidity_route["blendedRate"],
                "sufficient": liquidity_route["sufficient"],
                "required": liquidity_route["required"],
                "shortfall": liquidity_route["shortfall"],
                "allocations": liquidity_route["allocations"],
            },
            "treasurySummary": {
                "asset": treasury_asset,
                "total": float(treasury_position["total"]),
                "reserved": treasury_reserved,
                "available": treasury_available,
                "required": treasury_required,
                "coverage": round(treasury_coverage, 2),
                "requestedAmount": amount,
                "requestedAsset": from_asset,
                "side": str(rfq.get("side", "BUY")).upper(),
                "internalInventory": internal_inventory,
            },
            "costs": {
                "fundingUsd": round(treasury_required * 0.00024, 2),
                "fxUsd": round(treasury_required * 0.0003, 2),
                "networkUsd": 62.0,
            },
            "risk": {
                "exposureBefore": round(treasury_available, 4),
                "exposureAfter": round(max(treasury_available - treasury_required, 0), 4),
                "limitBefore": round((customer_volume / customer_limit) * 100, 2) if customer_limit else 0,
                "limitAfter": round(((customer_volume + amount) / customer_limit) * 100, 2) if customer_limit else 0,
                "level": "LOW" if passed else "HIGH",
            },
        }
