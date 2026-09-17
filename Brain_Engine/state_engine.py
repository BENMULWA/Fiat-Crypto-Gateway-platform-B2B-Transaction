import os
import uuid
import asyncio
from enum import Enum
from datetime import datetime
from pydantic import BaseModel, Field
from typing import List, Dict, Optional

from Brain_Engine.celo_integrations import corridor_api
from Brain_Engine.node_registry import (
    require_live,
    PROCUREMENT_WALLETS,
    CORRIDORS,
    corridor_eligible,
)
from Brain_Engine.risk_engine import (
    RiskLimitExceeded,
    check_daily_procurement_limit,
    check_min_balance,
    check_liquidity_threshold,
)
from Brain_Engine.Discovery_Engine import IMMDiscoveryEngine
from services.impala_airtime import impala_airtime
from services.safaricom_daraja import DarajaService
from cardano.usda import get_balance as get_cardano_usda_balance
from stellar_child_wallet import get_stellar_treasury_keypair
from workers.stellar_deposit_watcher import _get_server as get_stellar_server, USDC_ISSUER

daraja_service = DarajaService()
discovery_engine = IMMDiscoveryEngine()


def find_best_open_opportunity() -> Optional[dict]:
    """Ranks every corridor that is currently eligible (its own admin
    switch on, and both its procure/liquidate nodes live + enabled —
    corridor_eligible(), node_registry.py) by projected single-cycle
    yield, and returns the winner as a dict of the FSM config keys a
    cycle needs (node_procure/node_liquidate/discount/fx_edge), or None
    if nothing is open right now.

    This is deliberately the whole ranking mechanism today: real
    velocity/demand/liquidity/risk telemetry per node (the whitepaper's
    Liquidity Score) doesn't exist yet anywhere in this codebase, so
    "ranked" currently only differentiates by the discount/fx_edge each
    corridor is configured with. Swap in real per-node metrics here once
    that telemetry exists — this function is the one seam that needs to
    change, not the FSM around it."""
    open_corridors = [
        {"id": corridor_id, **corridor}
        for corridor_id, corridor in CORRIDORS.items()
        if corridor_eligible(corridor_id)
    ]
    if not open_corridors:
        return None

    ranked = sorted(
        open_corridors,
        key=lambda c: discovery_engine.project_corridor_yield(
            discount_rate=c["discount"], fx_edge_pct=c["fx_edge"], cycles=1
        )["single_cycle_multiplier"],
        reverse=True,
    )
    return ranked[0]


# How often a HALTED cycle re-checks for an open opportunity while parked
# in AWAITING_OPPORTUNITY. Real holds can last minutes to most of a day
# (see _evaluate_awaiting_opportunity's docstring) — this only bounds how
# often the FSM wakes up to look, not how long it's willing to wait.
AWAITING_OPPORTUNITY_POLL_SECONDS = float(os.getenv("AWAITING_OPPORTUNITY_POLL_SECONDS", "5"))


def _get_real_cardano_usda_balance() -> Optional[float]:
    """Real, live USDA balance in the shared Cardano custodial vault — the
    actual reserve N7's ledger-recognized mints are (or aren't) backed by.
    Returns None — not 0 — if the vault can't be reached or isn't
    configured in this environment, so callers can tell "confirmed empty"
    apart from "couldn't check." CardanoWallet() is constructed lazily
    here, not at module import time, so an environment without the Mamlaka
    vault secrets configured can still import this module (e.g. tests)."""
    try:
        from cardano.wallet import CardanoWallet
        wallet = CardanoWallet()
        return get_cardano_usda_balance(wallet.address_str)["usda"]
    except Exception as e:
        print(f"  ↳ ⚠️  Could not reach real Cardano USDA vault: {e}")
        return None


def _get_real_stellar_usdc_balance() -> Optional[float]:
    """Real, live USDC balance held by the Stellar treasury account — same
    "None means couldn't check, not zero" convention as the Cardano helper
    above."""
    try:
        server = get_stellar_server()
        treasury = get_stellar_treasury_keypair()
        account = server.accounts().account_id(treasury.public_key).call()
        for b in account.get("balances", []):
            if b.get("asset_code") == "USDC" and b.get("asset_issuer") == USDC_ISSUER:
                return float(b["balance"])
        return 0.0  # account exists on-chain but holds no USDC (no trustline, or empty)
    except Exception as e:
        print(f"  ↳ ⚠️  Could not reach real Stellar treasury: {e}")
        return None

# =====================================================================
# 1. SCHEMAS & DATA MODELS
# =====================================================================

class TransactionType(str, Enum):
    PROCURE = "PROCURE"
    LIQUIDATE = "LIQUIDATE"
    MINT = "MINT"
    TRANSFER = "TRANSFER"
    CELO_EXIT = "CELO_EXIT"
    SYSTEM_FUND = "SYSTEM_FUND"

class FSMState(str, Enum):
    IDLE = "IDLE"
    PROCURE = "PROCURE"     # State 1
    LIQUIDATE = "LIQUIDATE" # State 2
    MINT = "MINT"           # State 3
    ROLLOVER = "ROLLOVER"   # State 4
    AWAITING_OPPORTUNITY = "AWAITING_OPPORTUNITY"  # State 4b: holds between cycles
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
    external_ref: Optional[str] = None  # real provider receipt / on-chain tx hash, when this leg made an external call
    vault_backed: Optional[bool] = None  # MINT only: does a real reserve (Cardano USDA vault) actually cover this? None = not applicable/unknown

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

    async def get_node_volume_today(self, node_id: str, txn_type: "TransactionType") -> float:
        """Sums internal_usd_value of every `txn_type` leg credited TO
        node_id since UTC midnight — the basis for enforcing
        Node.daily_limit_usd (see risk_engine.py). Deliberately UTC-midnight
        rather than a rolling 24h window: predictable for ops/compliance to
        reason about ("today's volume"), and cheap to query without a
        separate "first txn timestamp" lookup."""
        since = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        txn_type_value = txn_type.value if hasattr(txn_type, "value") else txn_type

        if self.collection is not None:
            pipeline = [
                {"$match": {"to_node": node_id, "txn_type": txn_type_value, "timestamp": {"$gte": since}}},
                {"$group": {"_id": None, "total": {"$sum": "$internal_usd_value"}}},
            ]
            cursor = self.collection.aggregate(pipeline)
            result = await cursor.to_list(length=1)
            return result[0]["total"] if result else 0.0

        return sum(
            r.internal_usd_value
            for r in self.local_records
            if r.to_node == node_id and r.txn_type == txn_type and r.timestamp >= since
        )

    async def get_vault_backed_total(self, node_id: str, asset: str) -> float:
        """Sums only the MINT legs credited to node_id that were tagged
        vault_backed=True — i.e. real capacity already claimed by a
        genuinely-backed mint. Deliberately NOT the same as get_balance():
        get_balance() includes every legacy/simulation-era MINT ever
        written (vault_backed False or None), which for N7 is currently
        ~10,356 USDA of unbacked history that can never be un-minted from
        an append-only ledger. Gating new mints against that polluted
        total would permanently halt minting forever, even once real
        vault capacity is available — gating against only the backed
        total instead means real capacity tracks the real vault, un-
        affected by the pre-existing synthetic balance."""
        if self.collection is not None:
            pipeline = [
                {"$match": {
                    "to_node": node_id, "asset": asset,
                    "txn_type": TransactionType.MINT.value, "vault_backed": True,
                }},
                {"$group": {"_id": None, "total": {"$sum": "$internal_usd_value"}}},
            ]
            cursor = self.collection.aggregate(pipeline)
            result = await cursor.to_list(length=1)
            return result[0]["total"] if result else 0.0

        return sum(
            r.internal_usd_value
            for r in self.local_records
            if r.to_node == node_id and r.asset == asset
            and r.txn_type == TransactionType.MINT and r.vault_backed is True
        )

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

        # Stable identity for this corridor run, used to derive deterministic
        # external transaction IDs (see _execute_procure/_execute_liquidate)
        # instead of a fresh random UUID per call. Defaults to a random ID
        # when the caller doesn't supply one — identical to today's
        # behavior — but a caller that DOES pass the same run_id on a retry
        # (e.g. after a crash mid-cycle) gets the same PROCURE/LIQUIDATE
        # transaction IDs back, letting the provider's own idempotency
        # handling catch the duplicate instead of double-spending real
        # airtime or double-paying a real M-Pesa payout.
        self.run_id = config.get("run_id") or uuid.uuid4().hex[:12].upper()

        # resume_* lets a caller reconstruct an FSM mid-run from persisted
        # state (see workers/corridor_worker.py) — a resumed run continues
        # from exactly where the process stopped tracking it (cycle,
        # principal, KES float, and current FSMState) instead of starting
        # over from cycle 1. Omitting all of these is the normal fresh-run
        # path every existing caller already uses, unaffected.
        self.current_cycle = config.get("resume_cycle", 1)
        self.max_cycles = config.get("cycles", 5)

        self.current_usd_principal = config.get("resume_principal_usd", starting_capital_usd)
        self.current_kes_float = config.get("resume_kes_float", 0.0)
        self.halt_reason: Optional[str] = None
        
        # Dynamic Math Vectors
        self.BASE_RATE = config.get("baseline_rate", 129.50)
        self.DISCOUNT = config.get("discount", 0.06) 
        self.FX_EDGE = config.get("fx_edge", 0.0)
        self.INTERNAL_FX_RATE = self.BASE_RATE * (1 - self.FX_EDGE)
        
        # Dynamic Routing Nodes
        self.NODE_PROCURE = config.get("node_procure", "N2") # Default to Airtel
        self.NODE_LIQ = config.get("node_liquidate", "N5")   # Default to Mpesa
        self.NODE_MINT = "N7"
        self.NODE_EXIT = "N9"

        # N9 is the "multi-chain router" per the original IMM whitepaper —
        # which chain it actually settles to is a per-run choice, not fixed
        # to Celo. Defaults to "celo" so every existing caller (bot.py,
        # treasury.py's /corridor/execute-hft, none of which pass this key
        # today) keeps its exact current behavior unchanged.
        self.EXIT_CHAIN = config.get("exit_chain", "celo")

        # Injected external dependencies — default to the real module-level
        # singletons/functions so every existing caller (bot.py,
        # treasury.py's /corridor/execute-hft) is unaffected. A caller that
        # DOES override these (Brain_Engine/simulate.py, for a mocked demo
        # run) gets fully isolated fakes instead of monkeypatching the
        # shared singletons in place — mutating those in place would be a
        # real hazard the moment a simulated run and a real one are ever
        # in flight on the same server at the same time.
        self._send_airtime_fn = config.get("send_airtime_fn", impala_airtime.send_airtime)
        self._get_merchant_balance_fn = config.get("get_merchant_balance_fn", daraja_service.get_merchant_balance)
        self._get_vault_balance_fn = config.get("get_vault_balance_fn", _get_real_cardano_usda_balance)
        self._celo_swap_fn = config.get("celo_swap_fn", corridor_api.execute_celo_dex_swap)
        self._find_opportunity_fn = config.get("find_opportunity_fn", find_best_open_opportunity)
        self._poll_seconds = config.get("poll_seconds", AWAITING_OPPORTUNITY_POLL_SECONDS)

        # Simulation runs route through mocked nodes on purpose (to
        # demonstrate corridors that aren't actually live yet, e.g.
        # Telkom/N1) — skip the real liveness gate for those; every real
        # run still fails fast at construction, not mid-cycle, against a
        # node with no real external integration.
        self.simulate = bool(config.get("simulate", False))
        if not self.simulate:
            require_live(self.NODE_PROCURE)
            require_live(self.NODE_EXIT)

        # Applied last so a resumed run's FSMState always wins over the
        # IDLE default set above, regardless of anything else in __init__.
        resume_state = config.get("resume_state")
        if resume_state:
            self.state = FSMState(resume_state)

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

        elif self.state == FSMState.AWAITING_OPPORTUNITY:
            await self._evaluate_awaiting_opportunity()

        elif self.state == FSMState.CELO_EXIT:
            await self._execute_exit()

    async def _transition_to(self, new_state: FSMState):
        self.state = new_state
        # Simulating Database write latency for visual effect (non-blocking)
        await asyncio.sleep(0.3) 

    # --- STATE 1: PROCURE ---
    async def _execute_procure(self):
        """Master Wallet -> Airtime Node. Capture wholesale discount.

        Real call: disburses live airtime via ImpalaPay
        (services.impala_airtime.impala_airtime) — the only real airtime
        provider this platform purchases from. Mam-laka/Lipad (DarajaService)
        is a separate M-Pesa payment gateway, not an airtime source, and must
        never stand in here (see the explicit warning in impala_airtime.py).
        """
        print(f"\n[CYCLE {self.current_cycle}] STATE 1: PROCURE via {self.NODE_PROCURE}")

        wallet_phone = PROCUREMENT_WALLETS.get(self.NODE_PROCURE)
        if not wallet_phone:
            self.halt_reason = f"No live procurement wallet configured for {self.NODE_PROCURE}"
            print(f"  ↳ ❌ {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        # Risk Engine: refuse to spend real money through this node past its
        # configured daily cap, or take N7's real ledger balance below its
        # floor, before anything is disbursed.
        try:
            await check_daily_procurement_limit(
                self.ledger, self.NODE_PROCURE, self.current_usd_principal, TransactionType.PROCURE
            )
            await check_min_balance(self.ledger, self.NODE_MINT, self.current_usd_principal)
        except RiskLimitExceeded as e:
            self.halt_reason = f"RISK LIMIT: {e}"
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        # Inflated face value the discount lets us disburse for the same KES float
        airtime_value_kes = (self.current_usd_principal * self.BASE_RATE) / (1 - self.DISCOUNT)
        # Deterministic, not random: same run_id + cycle always produces the
        # same txn_id, so a retry of this exact leg (e.g. after a crash)
        # reuses it instead of minting a fresh one — see run_id's docstring.
        txn_id = f"B2B-{self.run_id}-C{self.current_cycle}"

        try:
            # 🚀 EXTERNAL API CALL: Buy Airtime (real, live — ImpalaPay)
            receipt = await asyncio.to_thread(
                self._send_airtime_fn,
                phone=wallet_phone,
                amount_kes=int(airtime_value_kes),
                reference=txn_id,
            )
        except Exception as e:
            self.halt_reason = f"EXTERNAL API FAILED: {e}"
            print(f"  ↳ ❌ {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        receipt_id = receipt.get("receipt_id") or txn_id

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
            txn_type=TransactionType.PROCURE, cycle=self.current_cycle,
            external_ref=receipt_id
        ))
        
        self.current_kes_float = airtime_value_kes
        print(f"  ↳ Captured {airtime_value_kes:,.2f} KES Airtime Value from ${self.current_usd_principal:,.2f} USDA")
        await self._transition_to(FSMState.LIQUIDATE)

    # --- STATE 2: LIQUIDATE ---
    async def _execute_liquidate(self):
        """Airtime Node -> Mobile Money Float. Internal Fiat Realization.

        Internal market making, not a buy-back from an external customer:
        the IMM already holds both the procured airtime AND the paybill
        it operates, so there is no external counterparty to pull KES
        from every cycle. LIQUIDATE instead recognizes the airtime's
        discounted KES value directly against money already sitting on
        the real Mam-laka merchant paybill — gated by a real balance
        check (check_liquidity_threshold, risk_engine.py) so a cycle can
        never recognize KES the paybill doesn't actually hold. This
        replaces the earlier STK-push-per-cycle design, which (a) doesn't
        match "internal" market making and (b) only ever confirms Mam-laka
        delivered a PIN prompt, not that the money landed — see git
        history on this method for that version.
        """
        print(f"[CYCLE {self.current_cycle}] STATE 2: LIQUIDATE via {self.NODE_LIQ} (internal paybill recognition)")

        try:
            # 🚀 EXTERNAL API CALL: real, live read of the Mam-laka merchant
            # paybill balance — no funds move here, this only verifies the
            # KES this cycle is about to recognize is real money already on
            # the paybill, not fabricated ledger value.
            balance_response = await asyncio.to_thread(self._get_merchant_balance_fn)
            check_liquidity_threshold(balance_response, self.current_kes_float, self.NODE_LIQ)
        except RiskLimitExceeded as e:
            self.halt_reason = f"RISK LIMIT: {e}"
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return
        except Exception as e:
            self.halt_reason = f"EXTERNAL API FAILED: {e}"
            print(f"  ↳ ❌ {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        print(f"  ↳ ✅ Paybill holds sufficient real KES to back {self.current_kes_float:,.2f} KES of airtime")

        await self.ledger.append(LedgerEntry(
            from_node=self.NODE_PROCURE, to_node=self.NODE_LIQ, asset="KES",
            amount=self.current_kes_float, internal_usd_value=self.current_usd_principal,
            txn_type=TransactionType.LIQUIDATE, cycle=self.current_cycle,
            external_ref=None,
        ))

        print(f"  ↳ Liquidated Airtime to {self.current_kes_float:,.2f} KES Float (internal, no external transfer)")
        await self._transition_to(FSMState.MINT)

    # --- STATE 3: MINT ---
    async def _execute_mint(self):
        """Mobile Money Float -> USDA. Spread Capture."""
        print(f"[CYCLE {self.current_cycle}] STATE 3: MINT USDA")

        new_usda_amount = self.current_kes_float / self.INTERNAL_FX_RATE
        profit = new_usda_amount - self.current_usd_principal

        # Hard gate, not a tag: mint only what the real Cardano USDA vault
        # can actually cover. Gated against remaining REAL capacity
        # (get_vault_backed_total — only mints already tagged
        # vault_backed=True), not the raw ledger balance, which still
        # carries ~10,356 USDA of pre-existing simulation-era unbacked
        # mints from before this gate existed. Those stay in the ledger
        # (append-only, never edited) as historical record, but no longer
        # count toward what new mints are allowed to claim.
        real_vault_balance = await asyncio.to_thread(self._get_vault_balance_fn)
        if real_vault_balance is None:
            self.halt_reason = "Could not reach the real Cardano USDA vault — refusing to mint blind."
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        already_backed = await self.ledger.get_vault_backed_total(self.NODE_MINT, "USDA")
        remaining_capacity = real_vault_balance - already_backed
        if new_usda_amount > remaining_capacity:
            self.halt_reason = (
                f"Real vault capacity exhausted: vault holds {real_vault_balance:,.2f} USDA, "
                f"{already_backed:,.2f} already claimed by prior real-backed mints, "
                f"{remaining_capacity:,.2f} remaining — this mint needs {new_usda_amount:,.4f}. "
                f"Fund the vault or reduce cycle size."
            )
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        print(f"  ↳ ✅ Vault-backed: {new_usda_amount:,.4f} USDA of {remaining_capacity:,.2f} remaining real capacity")

        await self.ledger.append(LedgerEntry(
            from_node=self.NODE_LIQ, to_node=self.NODE_MINT, asset="USDA",
            amount=new_usda_amount, internal_usd_value=new_usda_amount,
            txn_type=TransactionType.MINT, cycle=self.current_cycle,
            vault_backed=True,
        ))

        print(f"  ↳ Minted ${new_usda_amount:,.4f} USDA. (Profit: +${profit:,.4f})")
        self.current_usd_principal = new_usda_amount
        await self._transition_to(FSMState.ROLLOVER)

    # --- STATE 4: THE ROLLOVER GATE ---
    async def _evaluate_rollover(self):
        """Checks cycle limits to prevent early blockchain gas fees.

        Below max_cycles, this no longer loops straight back to PROCURE —
        it hands off to AWAITING_OPPORTUNITY, which only starts the next
        cycle once the Decision Engine actually has an open, ranked
        opportunity to route through (see that method's docstring)."""
        if self.current_cycle < self.max_cycles:
            print(f"[CYCLE {self.current_cycle}] STATE 4: ROLLOVER GATE ↻ -> AWAITING NEXT OPPORTUNITY")
            await self._transition_to(FSMState.AWAITING_OPPORTUNITY)
        else:
            print(f"[CYCLE {self.current_cycle}] STATE 4: ROLLOVER GATE 🔓 -> OPENING EXIT")
            await self._transition_to(FSMState.CELO_EXIT)

    # --- STATE 4b: AWAITING OPPORTUNITY (holds between cycles) ---
    async def _evaluate_awaiting_opportunity(self):
        """Parks the corridor between cycles until the Decision Engine
        finds an open, ranked opportunity among the currently live nodes —
        this is the actual "market maker" behavior: capital doesn't move
        just because a fixed timer says so, it moves when there's a real
        edge to capture. A hold can legitimately last anywhere from
        seconds to most of a day; this only controls how often the FSM
        re-checks (AWAITING_OPPORTUNITY_POLL_SECONDS), not how long it's
        willing to wait — a caller driving tick() in a loop (e.g. the
        terminal runner at the bottom of this file, or a background
        worker) will simply keep calling this over and over until an
        opportunity opens or the run is cancelled.

        current_usd_principal is untouched while holding: the next cycle
        starts with exactly what cycle N-1 minted, not fresh capital —
        compounding continues across the hold, it just doesn't advance in
        wall-clock time until there's somewhere real to route it."""
        opportunity = self._find_opportunity_fn()
        if not opportunity:
            print(f"[CYCLE {self.current_cycle}] STATE 4b: AWAITING OPPORTUNITY — nothing open, holding "
                  f"${self.current_usd_principal:,.4f} USDA, re-checking in {self._poll_seconds:.2f}s")
            await asyncio.sleep(self._poll_seconds)
            return  # stay in AWAITING_OPPORTUNITY; caller's tick loop will call again

        print(f"[CYCLE {self.current_cycle}] STATE 4b: OPPORTUNITY FOUND — {opportunity['id']} "
              f"({opportunity['node_procure']}->{opportunity['node_liquidate']}, "
              f"{opportunity['discount']*100:.0f}% discount) -> entering cycle {self.current_cycle + 1}")

        # Route the next cycle through whichever corridor actually won —
        # may differ from the corridor this run started on, per the
        # "ranked opportunities, not a fixed corridor" design.
        self.NODE_PROCURE = opportunity["node_procure"]
        self.NODE_LIQ = opportunity["node_liquidate"]
        self.DISCOUNT = opportunity["discount"]
        self.FX_EDGE = opportunity["fx_edge"]
        self.INTERNAL_FX_RATE = self.BASE_RATE * (1 - self.FX_EDGE)

        self.current_cycle += 1
        await self._transition_to(FSMState.PROCURE)

    # --- STATE 5: EXIT (multi-chain router) ---
    async def _execute_exit(self):
        """Dispatches to whichever chain this run's config named as
        EXIT_CHAIN. Named _execute_exit rather than _execute_celo_exit to
        reflect that N9 is the whitepaper's "multi-chain router," not a
        Celo-only node — Celo just remains the only chain with a real,
        working settlement mechanism today."""
        if self.EXIT_CHAIN == "stellar":
            await self._execute_stellar_exit()
        else:
            await self._execute_celo_exit()

    async def _execute_stellar_exit(self):
        """USDA -> USDC on Stellar. Real pre-flight balance check against
        the actual Stellar treasury account — confirmed live: it holds
        ~2 XLM (bare account-activation minimum) and no USDC trustline, so
        this will halt today, honestly, rather than fabricate a settlement.

        Unlike Celo (corridor_api.execute_celo_dex_swap, a real DEX swap
        that converts existing treasury holdings into USDC), there is no
        equivalent "self-mint" or swap mechanism wired for Stellar in this
        codebase — the treasury has nothing sellable to swap from even if
        one existed. Building that mechanism is a real product decision
        (a real Stellar DEX path? routing to a fixed external settlement
        address instead?) that shouldn't be invented silently here."""
        print(f"\n[FINAL] STATE 5: STELLAR EXIT via {self.NODE_EXIT}")

        real_usdc_balance = await asyncio.to_thread(_get_real_stellar_usdc_balance)
        if real_usdc_balance is None:
            self.halt_reason = "Could not verify the real Stellar treasury balance — refusing to exit blind."
            print(f"  ↳ ❌ {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        if real_usdc_balance < self.current_usd_principal:
            self.halt_reason = (
                f"Stellar treasury holds {real_usdc_balance:,.2f} real USDC, needs "
                f"{self.current_usd_principal:,.2f} to settle this exit — refusing to fabricate a settlement. "
                f"No real settlement mechanism is wired for Stellar yet either way (see docstring)."
            )
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        # Balance would be sufficient, but there's still no real settlement
        # call to make — see docstring. Halting rather than pretending.
        self.halt_reason = "Balance check passed, but no real Stellar settlement mechanism is wired yet."
        print(f"  ↳ ⚠️  {self.halt_reason}")
        self.state = FSMState.HALTED

    async def _execute_celo_exit(self):
        """USDA -> USDC. Final blockchain settlement."""
        print(f"\n[FINAL] STATE 5: CELO EXIT via {self.NODE_EXIT}")

        # Risk Engine: refuse to take N7's real ledger balance below its
        # floor before broadcasting the swap — checked before the external
        # call, not after, same as every other risk check in this file.
        try:
            await check_min_balance(self.ledger, self.NODE_MINT, self.current_usd_principal)
        except RiskLimitExceeded as e:
            self.halt_reason = f"RISK LIMIT: {e}"
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        try:
            # 🚀 EXTERNAL API CALL: Swap USDA to USDC on Celo Blockchain
            tx_hash = await self._celo_swap_fn(self.current_usd_principal)
            print(f"  ↳ 🔗 Blockchain Hash: {tx_hash}")
        except Exception as e:
            self.halt_reason = f"SMART CONTRACT FAILED: {e}"
            print(f"  ↳ ❌ {self.halt_reason}")
            self.state = FSMState.HALTED
            return
        
        await self.ledger.append(LedgerEntry(
            from_node=self.NODE_MINT, to_node=self.NODE_EXIT, asset="USDC",
            amount=self.current_usd_principal, internal_usd_value=self.current_usd_principal,
            txn_type=TransactionType.CELO_EXIT, cycle=self.current_cycle,
            external_ref=tx_hash
        ))
        
        print(f"  ↳ 🚀 SUCCESS: ${self.current_usd_principal:,.2f} USDC settled on Celo Blockchain.")
        await self._transition_to(FSMState.COMPLETED)

# =====================================================================
# 4. ASYNC EXECUTION SCRIPT (Terminal Runner)
# =====================================================================
async def main():
    print("===================================================")
    print(" MAMLAKA HFT DECISION ENGINE - INITIALIZING...")
    print(" ⚠️  PROCURE and CELO_EXIT now make REAL external calls")
    print(" (real airtime disbursement + real Celo settlement).")
    print("===================================================")

    import os
    if os.environ.get("IMM_LIVE_CONFIRM") != "YES":
        print("Refusing to run: this performs real airtime/Celo transactions.")
        print("Set IMM_LIVE_CONFIRM=YES to run this terminal script intentionally.")
        return

    # 1. Initialize the Immutable Ledger (Terminal Mode)
    master_ledger = ImmutableLedger()

    # 2. Instantiate the FSM with the agreed small test allocation (~5 KES)
    corridor_bot = HFTCorridorFSM(ledger=master_ledger, starting_capital_usd=5.0 / 129.50)
    await corridor_bot.boot_system()

    # 3. The "Tick" Loop (Runs until FSM completes or halts)
    while corridor_bot.state not in (FSMState.COMPLETED, FSMState.HALTED):
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