import asyncio
from Brain_Engine.state_engine import ImmutableLedger
from Brain_Engine.cache import memory_cache
from Brain_Engine.Discovery_Engine import IMMDiscoveryEngine

class DecisionEngine:
    def __init__(self):
        self.is_running = False
        self.total_portfolio_usd = 0.0
        self.discovery_api = IMMDiscoveryEngine() 

    async def scan_and_evaluate(self, ledger: ImmutableLedger):
        # Fetch live balances from the Immutable Ledger
        n1_airt = await ledger.get_balance("N1_TELKOM", "AIRTIME_KES")
        n2_airt = await ledger.get_balance("N2_AIRTEL", "AIRTIME_KES")
        n4_kes = await ledger.get_balance("N4_MPESA", "KES")
        n7_usda = await ledger.get_balance("N7_USDA", "USDA")
        
        self.total_portfolio_usd = ((n1_airt + n2_airt + n4_kes) / 130.50) + n7_usda
        
        n4_pct = (n4_kes / 130.50) / self.total_portfolio_usd if self.total_portfolio_usd > 0 else 1.0
        n7_pct = n7_usda / self.total_portfolio_usd if self.total_portfolio_usd > 0 else 1.0

        return {"N4_PCT": n4_pct, "N7_PCT": n7_pct, "N7_BAL": n7_usda}

    async def rank_opportunities(self) -> dict:
        print("   🔍 Scanning available live corridors...")
        
        airtel_data = self.discovery_api.project_corridor_yield(discount_rate=0.06, fx_edge_pct=0.0)
        telkom_data = self.discovery_api.project_corridor_yield(discount_rate=0.10, fx_edge_pct=0.05)
        
        opportunities = [
            {"name": "SAFARICOM/AIRTEL LIVE", "discount": 0.06, "roi": airtel_data["projected_profit_pct"], "is_live": True},
            {"name": "TELKOM SIMULATED", "discount": 0.10, "roi": telkom_data["projected_profit_pct"], "is_live": False}
        ]
        
        live_opps = [opp for opp in opportunities if opp["is_live"]]
        live_opps.sort(key=lambda x: x["roi"], reverse=True)
        
        winner = live_opps[0]
        print(f"   🏆 Highest LIVE ROI Opportunity: {winner['name']} (+{winner['roi']}%)")
        return winner

    async def start(self, db):
        self.is_running = True
        print("\n🟢 HFT Decision Engine Started: Scanning Matrix for LIVE Execution...")
        
        ledger = ImmutableLedger(db_collection=db["transactions"])
        
        while self.is_running:
            if memory_cache.get("system:kill_switch"):
                print("🛑 SYSTEM_HALT: Engine paused via Admin Kill Switch.") #
                await asyncio.sleep(5)
                continue

            node_health = await self.scan_and_evaluate(ledger)
            best_route = await self.rank_opportunities()
            target_roi = memory_cache.get("corridor:target_roi") or 1.0 

            if best_route["roi"] >= target_roi:
                live_trade_allocation_kes = 5.0 
                print(f"\n⚡ EXECUTE: Deploying {live_trade_allocation_kes} KES into {best_route['name']}...")
                
                try:
                    from Brain_Engine.corridor_1_airtime import AirtimeCeloCorridor
                    live_corridor = AirtimeCeloCorridor(db_collection=db["transactions"])
                    
                    result = await live_corridor.execute_from_kes(deployed_kes=live_trade_allocation_kes)
                    
                    print(f"✅ LIVE TRADE SUCCESS: +${result['profit_usda']:.4f} USDC Captured.")
                    print(f"🔗 Celo TxHash: {result['tx_hash']}")
                    
                    # 🟢 FIX: The Single-Shot Safety Lock
                    print("🛑 [SAFETY LOCK] Auto-tripping Kill Switch after 1 successful live trade to prevent float drain.")
                    memory_cache.set("system:kill_switch", True)
                    
                except Exception as e:
                    print(f"❌ LIVE EXECUTION FAILED: {str(e)}")
                    print("🛑 Tripping Global Kill Switch to protect funds.")
                    memory_cache.set("system:kill_switch", True)
                
                print("💤 Corridor Complete. Waiting for Admin to resume...")
                await asyncio.sleep(5)
            else:
                print(f"⏳ Engine Tick: Best route (+{best_route['roi']}%) is below threshold. Resting.")
                await asyncio.sleep(5)

    def stop(self):
        print("\n🛑 HFT Decision Engine Shutting Down...")
        self.is_running = False

hft_bot = DecisionEngine()