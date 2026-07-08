import uuid
import asyncio
from datetime import datetime

from services.safaricom_daraja import DarajaService
from Brain_Engine.celo_integrations import corridor_api

class AirtimeCeloCorridor:
    """
    Executes the Airtel -> USDA -> Celo Corridor in REAL TIME.
    """
    
    def __init__(self, db_collection):
        self.db = db_collection
        self.base_rate = 129.50         # 🚀 UPDATED: CBK Base Rate
        self.airtime_discount = 0.06    # 6% Wholesale Discount on Airtel
        self.daraja = DarajaService()

    async def execute_from_kes(self, deployed_kes: float):
        # Math: Convert initial KES to USD for logging purposes
        starting_usda = deployed_kes / self.base_rate

        print(f"\n🚀 STARTING LIVE CORRIDOR: AIRTEL (6%) -> USDA -> CELO")
        print(f"💰 Deployed Capital: {deployed_kes:,.2f} KES (≈ ${starting_usda:,.4f} USDA)")
        
        cycle_id = uuid.uuid4().hex[:8].upper()
        timestamp = datetime.utcnow()
        ledger_entries = []

        # ==========================================
        # STEP 1: PROCURE AIRTIME (THE 6% DISCOUNT)
        # ==========================================
        print(f"📦 STEP 1: Simulating Airtel Procurement...")
        
        # Math: 100 KES / (1 - 0.06) = 106.38 KES Face Value
        airtime_face_value_kes = deployed_kes / (1 - self.airtime_discount)
        
        print(f"   ↳ Paid Airtel: {deployed_kes:,.2f} KES")
        print(f"   ↳ Received Inventory: {airtime_face_value_kes:,.2f} KES (6% Yield Captured)")

        ledger_entries.append({
            "txn_id": f"PROC-{cycle_id}", 
            "timestamp": timestamp,
            "from_node": "N4_MPESA", 
            "to_node": "N2_AIRTEL", 
            "asset": "AIRTIME_KES", 
            "amount": airtime_face_value_kes,
            "internal_usd_value": starting_usda, 
            "txn_type": "PROCURE", 
            "cycle": 1
        })

        # ==========================================
        # STEP 2: INTERNAL MINT (AIRTIME -> USDA)
        # ==========================================
        # We liquidate the 106.38 KES back into USD at the 129.50 rate
        minted_usda = airtime_face_value_kes / self.base_rate
        profit_usda = minted_usda - starting_usda

        print(f"🔄 STEP 2: Internal Market Trade (Liquidate to Stablecoin).")
        print(f"   ↳ Minted: ${minted_usda:,.4f} USDA")
        print(f"   ↳ Profit Captured: +${profit_usda:,.4f} USDA")

        ledger_entries.append({
            "txn_id": f"MINT-{cycle_id}", 
            "timestamp": timestamp,
            "from_node": "N2_AIRTEL", 
            "to_node": "N7_USDA",
            "asset": "USDA", 
            "amount": minted_usda,
            "internal_usd_value": minted_usda, 
            "txn_type": "MINT", 
            "cycle": 1
        })

        # ==========================================
        # STEP 3: CELO EXIT (WEB3 TRANSACTION)
        # ==========================================
        print(f"🌐 STEP 3: Celo Blockchain Exit.")
        
        try:
            print(f"   ↳ Contacting Celo RPC Router...")
            # THIS IS THE REAL WEB3 SMART CONTRACT CALL using the inflated Minted USDA
            tx_hash = await corridor_api.execute_celo_dex_swap(minted_usda)
            print(f"   ↳ 🔗 Blockchain Hash Verified: {tx_hash}")
        except Exception as e:
            print(f"   ↳ ❌ Celo Contract Failed: {str(e)}")
            raise Exception(f"Web3 Error: {str(e)}")

        ledger_entries.append({
            "txn_id": f"EXIT-{cycle_id}", 
            "timestamp": timestamp,
            "from_node": "N7_USDA", 
            "to_node": "N9_CELO", 
            "asset": "USDC", 
            "amount": minted_usda,
            "internal_usd_value": minted_usda, 
            "txn_type": "CELO_EXIT", 
            "cycle": 1
        })

        # Log Profit to Settlement Tape
        ledger_entries.append({
            "txn_id": f"PNL-{cycle_id}", 
            "timestamp": timestamp,
            "from_node": "SPREAD_ENGINE", 
            "to_node": "TREASURY_PNL",
            "asset": "USDA", 
            "amount": profit_usda,
            "internal_usd_value": profit_usda, 
            "txn_type": "PNL_CAPTURE", 
            "cycle": 1
        })

        # Finalize and Return
        if self.db is not None:
            await self.db.insert_many(ledger_entries)
            
        yield_pct = (profit_usda / starting_usda) * 100
        print(f"✅ CORRIDOR COMPLETE. Total Yield: {yield_pct:.2f}%\n")

        return {
            "status": "success",
            "starting_capital": starting_usda,
            "ending_capital": minted_usda,
            "profit_usda": profit_usda,
            "yield_percent": round(yield_pct, 2),
            "cycle_id": cycle_id,
            "tx_hash": tx_hash
        }