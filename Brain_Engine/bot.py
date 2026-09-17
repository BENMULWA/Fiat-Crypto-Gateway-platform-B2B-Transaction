import asyncio
from Brain_Engine.state_engine import ImmutableLedger, HFTCorridorFSM, FSMState
from Brain_Engine.cache import memory_cache
from Brain_Engine.Discovery_Engine import IMMDiscoveryEngine
from Brain_Engine.node_registry import CORRIDORS, corridor_eligible

BASE_RATE = 129.50  # matches IMMDiscoveryEngine.baseline_rate_kes_usd and the FSM default

class DecisionEngine:
    def __init__(self):
        self.is_running = False
        self.total_portfolio_usd = 0.0
        self.discovery_api = IMMDiscoveryEngine()

    async def scan_and_evaluate(self, ledger: ImmutableLedger):
        # Fetch live balances from the Immutable Ledger, keyed by canonical
        # node_registry IDs (N2/N5/N7) — matches what HFTCorridorFSM writes.
        n2_airt = await ledger.get_balance("N2", "AIRTIME_KES")
        n5_kes = await ledger.get_balance("N5", "KES")
        n7_usda = await ledger.get_balance("N7", "USDA")

        self.total_portfolio_usd = ((n2_airt + n5_kes) / BASE_RATE) + n7_usda

        n5_pct = (n5_kes / BASE_RATE) / self.total_portfolio_usd if self.total_portfolio_usd > 0 else 1.0
        n7_pct = n7_usda / self.total_portfolio_usd if self.total_portfolio_usd > 0 else 1.0

        return {"N5_PCT": n5_pct, "N7_PCT": n7_pct, "N7_BAL": n7_usda}

    async def rank_opportunities(self) -> dict:
        print("   🔍 Scanning available live corridors...")

        # Only rank corridors that are eligible: the corridor switch itself
        # is on, and every node it routes through is both live (real
        # integration exists) and admin-enabled (node_registry.corridor_eligible).
        live_opps = []
        for corridor_id, c in CORRIDORS.items():
            if corridor_eligible(corridor_id):
                projection = self.discovery_api.project_corridor_yield(
                    discount_rate=c["discount"], fx_edge_pct=c["fx_edge"]
                )
                live_opps.append({**c, "id": corridor_id, "roi": projection["projected_profit_pct"]})

        if not live_opps:
            return {"name": "NONE", "roi": -1.0}

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
                starting_capital_usd = live_trade_allocation_kes / BASE_RATE
                print(f"\n⚡ EXECUTE: Deploying {live_trade_allocation_kes} KES into {best_route['name']} (full 5x rollover)...")

                try:
                    corridor = HFTCorridorFSM(
                        ledger=ledger,
                        starting_capital_usd=starting_capital_usd,
                        config={
                            "cycles": 5,
                            "baseline_rate": BASE_RATE,
                            "discount": best_route["discount"],
                            "fx_edge": best_route["fx_edge"],
                            "node_procure": best_route["node_procure"],
                            "node_liquidate": best_route["node_liquidate"],
                        },
                    )
                    await corridor.boot_system()
                    while corridor.state not in (FSMState.COMPLETED, FSMState.HALTED):
                        await corridor.tick()

                    if corridor.state == FSMState.HALTED:
                        raise Exception("Corridor halted mid-cycle — see logs above for the failing state.")

                    profit = corridor.current_usd_principal - starting_capital_usd
                    print(f"✅ LIVE 5x ROLLOVER SUCCESS: +${profit:.4f} USDC Captured.")

                    # 🟢 Single-Shot Safety Lock — unchanged from before: still requires
                    # manual admin re-arm between full corridor runs.
                    print("🛑 [SAFETY LOCK] Auto-tripping Kill Switch after 1 successful live 5x rollover to prevent float drain.")
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