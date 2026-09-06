import asyncio
import unittest

from datetime import datetime, timedelta

from dealer_engine.liquidity import SmartRouter
from routes.otc_admin import accept_dealer_rfq, create_dealer_rfq, execute_dealer_rfq, get_dealer_rfq_analysis, get_dealer_settlement, quote_dealer_rfq


class FakeCollection:
    def __init__(self):
        self.docs = []

    async def insert_one(self, document):
        document["_id"] = object()
        self.docs.append(document)
        return {"inserted_id": document["_id"]}

    async def find_one(self, query):
        for doc in self.docs:
            if query.get("id") is not None and doc.get("id") == query.get("id"):
                return doc
            if query.get("_id") is not None and doc.get("_id") == query.get("_id"):
                return doc
            if query.get("userId") is not None and doc.get("userId") == query.get("userId"):
                return doc
            if query.get("asset") is not None and doc.get("asset") == query.get("asset"):
                return doc
        return None

    def find(self, query=None):
        return self

    async def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            matches_id = query.get("id") is None or doc.get("id") == query.get("id")
            matches_asset = query.get("asset") is None or doc.get("asset") == query.get("asset")
            if matches_id and matches_asset:
                if "$set" in update:
                    doc.update(update["$set"])
                if "$inc" in update:
                    for key, value in update["$inc"].items():
                        doc[key] = doc.get(key, 0) + value
                return {"matched_count": 1, "modified_count": 1}
        if upsert:
            doc = dict(query)
            if "$set" in update:
                doc.update(update["$set"])
            self.docs.append(doc)
            return {"matched_count": 1}
        return {"matched_count": 0}

    def sort(self, *args, **kwargs):
        return self

    async def to_list(self, length=100):
        return self.docs


class FakeDB:
    def __getitem__(self, name):
        return getattr(self, name)

    def __init__(self):
        self.dealer_rfqs = FakeCollection()
        self.dealer_settlements = FakeCollection()
        self.dealer_executions = FakeCollection()
        self.treasury_rate_book = FakeCollection()
        self.treasury_positions = FakeCollection()
        self.liquidity_sources = FakeCollection()
        self.users = FakeCollection()
        self.retail_wallets = FakeCollection()
        self.users.docs.append({"_id": "CUST-01", "status": "active", "kycStatus": "verified"})
        self.treasury_positions.docs.append({"asset": "USDC", "total": 200000, "reserved": 0, "pending": 0})
        self.retail_wallets.docs.append({"userId": "CUST-01", "USDA": 200000})


class DealerRfqFlowTests(unittest.TestCase):
    def test_dealer_rfq_analysis_and_quote_flow(self):
        async def run_test():
            db = FakeDB()
            result = await create_dealer_rfq(
                {
                    "amount": 1000,
                    "from_asset": "USDC",
                    "to_asset": "KES",
                    "customer_id": "CUST-01",
                    "customer_name": "Acme",
                    "side": "BUY",
                    "settlement_channel": "BANK_TO_WALLET",
                },
                db,
            )

            self.assertEqual(result["status"], "success")
            self.assertTrue(result["rfq"]["analysis"]["passed"])
            treasury = result["rfq"]["analysis"]["treasurySummary"]
            self.assertEqual(treasury["asset"], "USDC")
            self.assertEqual(treasury["total"], 200000)
            self.assertEqual(treasury["required"], 1000)
            self.assertEqual(treasury["coverage"], 20000)

            rfq_id = result["rfq"]["id"]
            analysis = await get_dealer_rfq_analysis(rfq_id, db)
            self.assertEqual(analysis["status"], "success")
            self.assertTrue(analysis["analysis"]["passed"])

            quoted = await quote_dealer_rfq(rfq_id, {"spread_bps": 50}, db)
            self.assertEqual(quoted["status"], "success")
            self.assertGreater(quoted["rfq"]["quote"]["execution_rate"], 0)
            self.assertEqual(quoted["rfq"]["status"], "quoted")
            self.assertTrue(quoted["rfq"]["quote"]["route"])

            accepted = await accept_dealer_rfq(rfq_id, db, {"_id": "dealer-01", "role": "admin"})
            self.assertEqual(accepted["status"], "success")
            self.assertEqual(accepted["rfq"]["status"], "accepted")
            self.assertTrue(accepted["rfq"]["reservationId"])
            self.assertEqual(db.dealer_executions.docs[0]["status"], "accepted")

            executed = await execute_dealer_rfq(rfq_id, db)
            settlement_id = executed["settlement"]["id"]
            settlement = db.dealer_settlements.docs[0]
            settlement["createdAt"] = datetime.utcnow() - timedelta(seconds=5)
            completed = await get_dealer_settlement(settlement_id, db)
            self.assertEqual(completed["settlement"]["status"], "completed")
            self.assertEqual(completed["settlement"]["legs"]["fiat"]["status"], "confirmed")
            self.assertEqual(completed["settlement"]["legs"]["crypto"]["status"], "confirmed")

    def test_analysis_blocks_insufficient_treasury_inventory(self):
        async def run_test():
            db = FakeDB()
            db.treasury_positions.docs[0]["total"] = 1
            result = await create_dealer_rfq(
                {
                    "amount": 1000,
                    "from_asset": "USDC",
                    "to_asset": "KES",
                    "customer_id": "CUST-01",
                    "side": "BUY",
                    "settlement_channel": "BANK_TO_WALLET",
                },
                db,
            )
            self.assertFalse(result["rfq"]["analysis"]["passed"])
            self.assertEqual(result["rfq"]["status"], "blocked")
            self.assertFalse(next(check for check in result["rfq"]["analysis"]["treasury"] if check["key"] == "treasury_available")["passed"])

        asyncio.run(run_test())

    def test_smart_router_splits_required_amount_by_effective_cost(self):
        route = SmartRouter().route(
            [
                {"id": "lp-expensive", "name": "LP Expensive", "rate": 131, "fee": 0, "available": 100},
                {"id": "treasury", "name": "Internal Treasury", "rate": 130, "fee": 0, "available": 60},
                {"id": "lp-cheap", "name": "LP Cheap", "rate": 130.5, "fee": 0, "available": 40},
            ],
            90,
        )
        self.assertTrue(route["sufficient"])
        self.assertEqual(route["totalAllocated"], 90)
        self.assertEqual([item["sourceId"] for item in route["allocations"]], ["treasury", "lp-cheap"])


if __name__ == "__main__":
    unittest.main()
