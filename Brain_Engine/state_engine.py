import uuid
import asyncio
from enum import Enum
from datetime import datetime
from pydantic import BaseModel, Field
from typing import List, Dict, Optional

from Brain_Engine.celo_integrations import corridor_api

# =====================================================================
# 1. SCHEMAS & DATA MODELS
# =====================================================================

class TransactionType(str, Enum):
    PROCURE = "PROCURE"
    LIQUIDATE = "LIQUIDATE"
    MINT = "MINT"
    TRANSFER = "TRANSFER"
    CELO_EXIT = "CELO_EXIT"
    SYSTEM_FUND = "SYSTEM_c import corridor_apiFUND"

class FSMState(str, Enum):
    IDLE = "IDLE"
    PROCURE = "PROCURE"     # State 1
    LIQUIDATE = "LIQUIDATE" # State 2
    MINT = "MINT"           # State 3
    ROLLOVER = "ROLLOVER"   # State 4
    CELO_EXIT = "CELO_EXIT" # State 5
    COMPLETED = "COMPLETED"
    HALTED = "HALTED"

class LedgerEntry(BaseModel):
    txn_id: str = Field(default_factory=lambda: f"TXN-{uuid.uuid4().hex[:8].upper()}")
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    from_node: str
    to_node: str
    asset: str
    amount: float
    internal_usd_value: float
    txn_type: TransactionType
    cycle: int

class Node(BaseModel):
    id: str
    name: str
    asset: str
    category: str

# =====================================================================
# 2. THE IMMUTABLE LEDGER (MongoDB Ready)
# =====================================================================

class ImmutableLedger:
    """
    The Single Source of Truth.
    NO UPDATE COMMANDS ALLOWED. Balances are derived purely from sums.
    """
    def __init__(self, db_collection=None):
        # Pass your AsyncIOMotorCollection here in FastAPI production
        self.collection = db_collection 
        self.local_records: List[LedgerEntry] = [] # Fallback for Terminal Testing

    async def append(self, entry: LedgerEntry):
        if self.collection is not None:
            # Production: Write receipt directly to MongoDB
            await self.collection.insert_one(entry.dict())
        else:
            # Terminal: Keep in memory
            self.local_records.append(entry)
        
    async def get_balance(self, node_id: str, asset: str) -> float:
        """
        Calculates balance on the fly: SUM(Credits) - SUM(Debits)
        """
        if self.collection is not None:
            # Production: Blazing fast MongoDB Aggregate query
            pipeline = [
                {"$match": {"asset": asset, "$or": [{"from_node": node_id}, {"to_node": node_id}]}},
                {"$project": {
                    "amount": 1,
                    "is_credit": {"$eq": ["$to_node", node_id]},
                    "is_debit": {"$eq": ["$from_node", node_id]}
                }},
                {"$group": {
                    "_id": None,
                    "credits": {"$sum": {"$cond": ["$is_credit", "$amount", 0]}},
                    "debits": {"$sum": {"$cond": ["$is_debit", "$amount", 0]}}
                }}
            ]
            # Use to_list for motor async cursor
            cursor = self.collection.aggregate(pipeline)
            result = await cursor.to_list(length=1)
            if result:
                return result[0]["credits"] - result[0]["debits"]
            return 0.0
        else:
            # Terminal: Calculate from local array
            credits = sum(r.amount for r in self.local_records if r.to_node == node_id and r.asset == asset)
            debits = sum(r.amount for r in self.local_records if r.from_node == node_id and r.asset == asset)
            return credits - debits

# =====================================================================
# 3. THE CORRIDOR FINITE STATE MACHINE (FSM)
# =====================================================================

class HFTCorridorFSM:
    """
    Executes dynamic compounding strategies (e.g. N1->N4->N7->N9 or N2->N5->N7->N9).
    Strictly transitions through states to prevent gas-fee leakage.
    """
    def __init__(self, ledger: ImmutableLedger, starting_capital_usd: float, config: dict = None):
        self.ledger = ledger
        self.state = FSMState.IDLE
        
        # --- DYNAMIC CONFIGURATION INJECTED FROM THE DEALING DESK ---
        config = config or {}
        self.current_cycle = 1
        self.max_cycles = config.get("cycles", 5)
        
        self.current_usd_principal = starting_capital_usd
        self.current_kes_float = 0.0
        
        # Dynamic Math Vectors
        self.BASE_RATE = config.get("baseline_rate", 129.50)
        self.DISCOUNT = config.get("discount", 0.06) 
        self.FX_EDGE = config.get("fx_edge", 0.0)
        self.INTERNAL_FX_RATE = self.BASE_RATE * (1 - self.FX_EDGE)
        
        # Dynamic Routing Nodes
        self.NODE_PROCURE = config.get("node_procure", "N2") # Default to Airtel
        self.NODE_LIQ = config.get("node_liquidate", "N5")   # Default to Airtel Money
        self.NODE_MINT = "N7"
        self.NODE_EXIT = "N9"

    async def boot_system(self):
        # System Boot: Fund the Master Wallet (N7) secretly to start the machine
        await self.ledger.append(LedgerEntry(
            from_node="EXTERNAL", to_node=self.NODE_MINT, asset="USDA", 
            amount=self.current_usd_principal, internal_usd_value=self.current_usd_principal,
            txn_type=TransactionType.SYSTEM_FUND, cycle=0
        ))

    async def tick(self):
        """
        The heartbeat of the FSM. led continuously by the background worker.
        """
        if self.state == FSMState.COMPLETED or self.state == FSMState.HALTED:
            return

        if self.state == FSMState.IDLE:
            await self._transition_to(FSMState.PROCURE)

        elif self.state == FSMState.PROCURE:
            await self._execute_procure()
            
        elif self.state == FSMState.LIQUIDATE:
            await self._execute_liquidate()
            
        elif self.state == FSMState.MINT:
            await self._execute_mint()
            
        elif self.state == FSMState.ROLLOVER:
            await self._evaluate_rollover()
            
        elif self.state == FSMState.CELO_EXIT:
            await self._execute_celo_exit()

    async def _transition_to(self, new_state: FSMState):
        self.state = new_state
        # Simulating Database write latency for visual effect (non-blocking)
        await asyncio.sleep(0.3) 

    # --- STATE 1: PROCURE ---
    async def _execute_procure(self):
        """Master Wallet -> Airtime Node. Capture wholesale discount."""
        print(f"\n[CYCLE {self.current_cycle}] STATE 1: PROCURE via {self.NODE_PROCURE}")
        
        try:
            # 🚀 EXTERNAL API CALL: Buy Airtime
            airtime_value_kes = await corridor_api.buy_telkom_airtime(
                self.current_usd_principal, 
                self.DISCOUNT
            )
        except Exception as e:
            print(f"  ↳ ❌ EXTERNAL API FAILED: {e}")
            self.state = FSMState.HALTED
            return
        
        # 1. Debit Master Wallet
        await self.ledger.append(LedgerEntry(
            from_node=self.NODE_MINT, to_node="MARKET", asset="USDA",
            amount=self.current_usd_principal, internal_usd_value=self.current_usd_principal,
            txn_type=TransactionType.TRANSFER, cycle=self.current_cycle
        ))
        
        # 2. Credit Airtime Node
        await self.ledger.append(LedgerEntry(
            from_node="MARKET", to_node=self.NODE_PROCURE, asset="AIRTIME_KES",
            amount=airtime_value_kes, internal_usd_value=self.current_usd_principal,
            txn_type=TransactionType.PROCURE, cycle=self.current_cycle
        ))
        
        self.current_kes_float = airtime_value_kes
        print(f"  ↳ Captured {airtime_value_kes:,.2f} KES Airtime Value from ${self.current_usd_principal:,.2f} USDA")
        await self._transition_to(FSMState.LIQUIDATE)

    # --- STATE 2: LIQUIDATE ---
    async def _execute_liquidate(self):
        """Airtime Node -> Mobile Money Float. Fiat Realization."""
        print(f"[CYCLE {self.current_cycle}] STATE 2: LIQUIDATE via {self.NODE_LIQ}")
        
        try:
            # 🚀 EXTERNAL API CALL: Liquidate to Fiat
            await corridor_api.liquidate_to_fiat(self.current_kes_float)
        except Exception as e:
            print(f"  ↳ ❌ EXTERNAL API FAILED: {e}")
            self.state = FSMState.HALTED
            return
        
        await self.ledger.append(LedgerEntry(
            from_node=self.NODE_PROCURE, to_node=self.NODE_LIQ, asset="KES",
            amount=self.current_kes_float, internal_usd_value=self.current_usd_principal,
            txn_type=TransactionType.LIQUIDATE, cycle=self.current_cycle
        ))
        
        print(f"  ↳ Liquidated Airtime to {self.current_kes_float:,.2f} KES Float")
        await self._transition_to(FSMState.MINT)

    # --- STATE 3: MINT ---
    async def _execute_mint(self):
        """Mobile Money Float -> USDA. Spread Capture."""
        print(f"[CYCLE {self.current_cycle}] STATE 3: MINT USDA")
        
        new_usda_amount = self.current_kes_float / self.INTERNAL_FX_RATE
        profit = new_usda_amount - self.current_usd_principal
        
        await self.ledger.append(LedgerEntry(
            from_node=self.NODE_LIQ, to_node=self.NODE_MINT, asset="USDA",
            amount=new_usda_amount, internal_usd_value=new_usda_amount,
            txn_type=TransactionType.MINT, cycle=self.current_cycle
        ))
        
        print(f"  ↳ Minted ${new_usda_amount:,.4f} USDA. (Profit: +${profit:,.4f})")
        self.current_usd_principal = new_usda_amount
        await self._transition_to(FSMState.ROLLOVER)

    # --- STATE 4: THE ROLLOVER GATE ---
    async def _evaluate_rollover(self):
        """Checks cycle limits to prevent early blockchain gas fees."""
        if self.current_cycle < self.max_cycles:
            print(f"[CYCLE {self.current_cycle}] STATE 4: ROLLOVER GATE ↻ -> LOOPING BACK")
            self.current_cycle += 1
            await self._transition_to(FSMState.PROCURE)
        else:
            print(f"[CYCLE {self.current_cycle}] STATE 4: ROLLOVER GATE 🔓 -> OPENING EXIT")
            await self._transition_to(FSMState.CELO_EXIT)

    # --- STATE 5: CELO EXIT ---
    async def _execute_celo_exit(self):
        """USDA -> USDC. Final blockchain settlement."""
        print(f"\n[FINAL] STATE 5: CELO EXIT via {self.NODE_EXIT}")
        
        try:
            # 🚀 EXTERNAL API CALL: Swap USDA to USDC on Celo Blockchain
            tx_hash = await corridor_api.execute_celo_dex_swap(self.current_usd_principal)
            print(f"  ↳ 🔗 Blockchain Hash: {tx_hash}")
        except Exception as e:
            print(f"  ↳ ❌ SMART CONTRACT FAILED: {e}")
            self.state = FSMState.HALTED
            return
        
        await self.ledger.append(LedgerEntry(
            from_node=self.NODE_MINT, to_node=self.NODE_EXIT, asset="USDC",
            amount=self.current_usd_principal, internal_usd_value=self.current_usd_principal,
            txn_type=TransactionType.CELO_EXIT, cycle=self.current_cycle
        ))
        
        print(f"  ↳ 🚀 SUCCESS: ${self.current_usd_principal:,.2f} USDC settled on Celo Blockchain.")
        await self._transition_to(FSMState.COMPLETED)

# =====================================================================
# 4. ASYNC EXECUTION SCRIPT (Terminal Runner)
# =====================================================================
async def main():
    print("===================================================")
    print(" MAMLAKA HFT DECISION ENGINE - INITIALIZING...")
    print("===================================================")
    
    # 1. Initialize the Immutable Ledger (Terminal Mode)
    master_ledger = ImmutableLedger()
    
    # 2. Instantiate the FSM with $100 starting capital
    corridor_bot = HFTCorridorFSM(ledger=master_ledger, starting_capital_usd=100.00)
    await corridor_bot.boot_system()
    
    # 3. The "Tick" Loop (Runs until FSM completes)
    while corridor_bot.state != FSMState.COMPLETED:
        await corridor_bot.tick()
        
    print("\n===================================================")
    print(" LEDGER AUDIT (FINAL BALANCES)")
    print("===================================================")
    n1_bal = await master_ledger.get_balance('N1', 'AIRTIME_KES')
    n4_bal = await master_ledger.get_balance('N4', 'KES')
    n7_bal = await master_ledger.get_balance('N7', 'USDA')
    n9_bal = await master_ledger.get_balance('N9', 'USDC')
    
    print(f"N1 (Telkom Airtime):  {n1_bal:,.2f} KES")
    print(f"N4 (M-Pesa Float):    {n4_bal:,.2f} KES")
    print(f"N7 (Master USDA):     ${n7_bal:,.2f}")
    print(f"N9 (Celo USDC Exit):  ${n9_bal:,.2f}")
    
    print("\n🔍 TOTAL LEDGER ENTRIES WRITTEN:", len(master_ledger.local_records))

if __name__ == "__main__":
    asyncio.run(main())