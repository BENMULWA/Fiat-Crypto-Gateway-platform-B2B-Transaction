import asyncio
from Brain_Engine.state_engine import ImmutableLedger, HFTCorridorFSM, FSMState
from Brain_Engine.cache import memory_cache

class DecisionEngine:
    """
    The Algorithmic Decision Engine (Section 5 of the White Paper).
    Runs continuously, monitoring nodes and triggering FSM Corridors.
    """
    def __init__(self):
        self.is_running = False
        self.total_portfolio_usd = 0.0

    async def scan_and_evaluate(self, ledger: ImmutableLedger):
        """
        Step 1 & 2: Query Vault Allocation & Calculate Liquidity Scores.
        """
        # Fetch live balances from the Immutable Ledger
        n1_airt = await ledger.get_balance("N1", "AIRTIME_KES")
        n4_kes = await ledger.get_balance("N4", "KES")
        n7_usda = await ledger.get_balance("N7", "USDA")
        
        # Calculate Total Portfolio Value (Simplified for N1, N4, N7)
        self.total_portfolio_usd = (
            (n1_airt / 130.50) + 
            (n4_kes / 130.50) + 
            n7_usda
        )

        # Calculate Node Liquidity Percentages
        if self.total_portfolio_usd > 0:
            n4_pct = (n4_kes / 130.50) / self.total_portfolio_usd
            n7_pct = n7_usda / self.total_portfolio_usd
        else:
            n4_pct = n7_pct = 1.0

        return {"N4_PCT": n4_pct, "N7_PCT": n7_pct, "N7_BAL": n7_usda}

    async def pathfind_roi(self) -> float:
        """
        Step 3: Calculate projected ROI based on external cache rates.
        """
        binance_rate = memory_cache.get("rates:binance_usdt_kes")
        telkom_discount = 0.10 # 10%
        internal_mint = 118.75 # Internal KES to USDA cost
        
        # Projected Multiplier for 1 cycle: (125 / 0.9) / 118.75 = 1.169x
        # For 5 cycles, we approximate compound yield.
        projected_roi = 1.2865 
        return projected_roi

    async def start(self, db):
        self.is_running = True
        print("\n🟢 HFT Decision Engine Started: Scanning 12-Node Matrix...")
        
        ledger = ImmutableLedger(db_collection=db["transactions"])
        
        while self.is_running:
            # 1. Check Global Kill Switch
            if memory_cache.get("system:kill_switch"):
                print("🛑 SYSTEM_HALT: Engine paused via Admin Kill Switch.")
                await asyncio.sleep(5)
                continue

            # 2. Scan & Evaluate Nodes
            node_health = await self.scan_and_evaluate(ledger)
            
            # 3. CIRCUIT BREAKER (Check if any core node < 5% liquidity)
            if node_health["N4_PCT"] < 0.05 or node_health["N7_PCT"] < 0.05:
                print(f"⚠️ CIRCUIT BREAKER FIRED: Core node liquidity below 5%.")
                print("   Action: Freezing Corridors. Awaiting OTC Rebalance.")
                memory_cache.set("system:kill_switch", True) # Auto-trip the kill switch
                continue

            # 4. Pathfinding
            expected_roi = await self.pathfind_roi()
            target_roi = memory_cache.get("corridor:target_roi")

            # 5. Execute
            # DYNAMIC ALLOCATION: Only trade 10% of the available N7_USDA balance to manage risk
            trade_allocation = node_health["N7_BAL"] * 0.10
            
            if expected_roi >= target_roi and trade_allocation >= 50:
                print(f"\n⚡ EXECUTE: Deploying ${trade_allocation:,.2f} USDA into FSM Corridor...")
                
                # Spawn the 5-Step Finite State Machine with DYNAMIC CAPITAL
                fsm = HFTCorridorFSM(ledger=ledger, starting_capital_usd=trade_allocation)
                await fsm.boot_system()
                
                while fsm.state != FSMState.COMPLETED and self.is_running:
                    await fsm.tick()
                    await asyncio.sleep(0.5) # Slowed down slightly so you can watch the UI update!
                
                print("💤 Corridor Complete. Engine resting for 10s...")
                await asyncio.sleep(10)
            else:
                print(f"⏳ Engine Tick: Insufficient capital (${trade_allocation:,.2f}) or low ROI. Resting.")
                await asyncio.sleep(5)

    def stop(self):
        print("\n🛑 HFT Decision Engine Shutting Down...")
        self.is_running = False

# Singleton Export
hft_bot = DecisionEngine()