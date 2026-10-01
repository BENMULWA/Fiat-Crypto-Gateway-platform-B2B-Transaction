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
    LIQUIDATION_WALLETS,
    CORRIDORS,
    corridor_eligible,
)
from Brain_Engine.risk_engine import (
    RiskLimitExceeded,
    check_daily_procurement_limit,
    check_min_balance,
    check_liquidity_threshold,
    check_discount_floor,
    check_airtime_backing,
    check_swap_slippage,
    check_gas_covers_margin,
)
from Brain_Engine.Discovery_Engine import IMMDiscoveryEngine
from services.impala_airtime import impala_airtime
from services.safaricom_daraja import DarajaService
from services.comet_client import CometClient
from services import rate_feed
from cardano.usda import get_balance as get_cardano_usda_balance
from stellar_child_wallet import get_stellar_treasury_keypair
from workers.stellar_deposit_watcher import _get_server as get_stellar_server, USDC_ISSUER

daraja_service = DarajaService()
discovery_engine = IMMDiscoveryEngine()
comet_client = CometClient()


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


def _get_real_treasury_usdt_balance() -> float:
    """Real, live USDT balance of this backend's Celo treasury wallet —
    same pattern/contract as routes.treasury._read_celo_usdc_balance_sync
    and _read_celo_imc_balance_sync, duplicated locally rather than
    imported from routes.treasury to avoid pulling a FastAPI route module
    (auth, database deps, etc.) into Brain_Engine for one balance read.
    _execute_mint_comet's internal IMC->USDT leg checks this before
    crediting any USDT out, so the corridor never promises USDT the
    treasury doesn't actually hold."""
    from routes.valora import ASSET_CONTRACTS, get_treasury_address, w3 as celo_w3
    balance_abi = [{
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }]
    treasury_address = get_treasury_address()
    contract = celo_w3.eth.contract(address=ASSET_CONTRACTS["USDT"], abi=balance_abi)
    return float(contract.functions.balanceOf(treasury_address).call()) / (10 ** 6)


def _get_real_treasury_imc_balance() -> float:
    """Real, live IMC balance of this backend's Celo treasury wallet.
    _check_manual_mint polls this before/after a manual Comet-dashboard
    mint to detect the real on-chain delta — see that method's docstring
    for why this replaced a typed-in "I minted N IMC" confirmation."""
    from routes.valora import ASSET_CONTRACTS, get_treasury_address, w3 as celo_w3
    balance_abi = [{
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }]
    treasury_address = get_treasury_address()
    contract = celo_w3.eth.contract(address=ASSET_CONTRACTS["IMC"], abi=balance_abi)
    return float(contract.functions.balanceOf(treasury_address).call()) / (10 ** 6)

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
    AWAITING_MANUAL_TOPUP = "AWAITING_MANUAL_TOPUP"  # State 1b: automated STK top-up failed/unconfirmed; holds until a real payout-balance increase is observed
    MINT = "MINT"           # State 3
    AWAITING_MANUAL_MINT = "AWAITING_MANUAL_MINT"  # State 3b: Comet rejected tenant-triggered tokenize; holds until a real on-chain balance increase is observed
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

        # Per-cycle / per-run state that workers/corridor_worker.py persists
        # after every tick (exitPathChosen, cyclePnl, cumulativePnl) — see
        # that file's _persist_tick. Resumable the same way everything else
        # here is: a caller reconstructing a run mid-flight can pass these
        # back in via config.
        self.cumulative_pnl_usd: float = config.get("resume_cumulative_pnl_usd", 0.0)
        self.last_cycle_pnl_usd: float = 0.0
        self.exit_path_chosen: Optional[str] = config.get("resume_exit_path_chosen")
        
        # Dynamic Math Vectors
        self.BASE_RATE = config.get("baseline_rate", 129.50)
        self.DISCOUNT = config.get("discount", 0.05)  # 0.05 = real confirmed Airtel reseller rate; every real caller passes it explicitly from node_registry.CORRIDORS anyway 
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

        # --- Comet/IMC corridor variant (all default to today's unchanged
        # Cardano/native-Celo behavior; a caller opts in explicitly) ---
        # mint_provider: "cardano" (default, _execute_mint / USDA vault) or
        # "comet" (_execute_mint_comet / real KES->IMC swap via Comet).
        self.MINT_PROVIDER = config.get("mint_provider", "cardano")
        # exit_provider, checked before EXIT_CHAIN's native-chain dispatch:
        # "native" (default, today's _execute_celo_exit/_execute_stellar_exit),
        # "comet" (IMC->USDC via Comet's Celo AMM), or "kes" (real cash payout).
        self.EXIT_PROVIDER = config.get("exit_provider", "native")

        # Live wholesale-discount lookup (Brain_Engine/risk_engine.check_discount_floor).
        # Off by default so the existing airtel_5x/telkom_5x corridors keep
        # trusting their static CORRIDORS[...]["discount"] value exactly as
        # today — only a caller that explicitly opts in pays a live API
        # round-trip and a possible floor-breach halt per cycle.
        self.USE_LIVE_DISCOUNT = bool(config.get("use_live_discount", False))
        self.DISCOUNT_FLOOR = config.get("discount_floor", 0.035)

        # KES-exit path (Step 3/4 rate comparison from the spec): off by
        # default. When enabled, _evaluate_rollover compares cashing out to
        # KES now against continuing to compound, using the live base rate.
        self.ENABLE_KES_EXIT_PATH = bool(config.get("enable_kes_exit_path", False))
        self.REINVEST_THRESHOLD_KES = config.get("reinvest_threshold_kes", 0.0)

        # Reseed/sweep split at final exit (only meaningful for
        # exit_provider="comet"): how much of the closing position gets
        # swapped back to KES to refund tomorrow's procurement float. 0
        # (default) means "exit everything, reseed manually" — today's
        # unchanged behavior.
        self.FLOOR_KES = config.get("floor_kes", 0.0)

        # Rough, configurable placeholder for real Celo gas cost in USD —
        # checked against cumulative_pnl_usd before the native Celo exit
        # broadcasts (check_gas_covers_margin). Not a live gas quote; a
        # real one would need celo_integrations' existing gas-estimation
        # logic exposed to a pre-flight caller instead of only running
        # inside _sync_celo_transfer right before it signs.
        self.ESTIMATED_GAS_COST_USD = config.get("estimated_gas_cost_usd", 0.01)

        # Numeric Comet user id the corridor's own treasury position posts
        # under (docs.mamlakapsp.com/api/tokenization.html's userId, docs.
        # mamlakapsp.com/api/amm.html's userId — both require a real
        # integer, not the string run_id this FSM otherwise uses for
        # idempotency). Only required when mint_provider/exit_provider is
        # "comet" — required, not defaulted to a made-up value, so a
        # missing/unconfigured treasury account fails loudly instead of
        # silently posting under an arbitrary id.
        comet_user_id = config.get("comet_user_id") or os.getenv("COMET_TREASURY_USER_ID")
        self.COMET_USER_ID = int(comet_user_id) if comet_user_id else None

        # Real, self-custodied Celo wallet the Comet exit withdraws to
        # after the USDT->USDC swap — same address and same env var the
        # native _execute_celo_exit already sends to (celo_integrations.py's
        # CELO_EXIT_ADDRESS), so both corridor variants land in the one
        # wallet you actually control, not scattered across Comet's
        # custodial system.
        self.COMET_EXIT_ADDRESS = config.get("comet_exit_address") or os.getenv("CELO_EXIT_ADDRESS")

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
        # celo_transfer_fn (generic ERC-20 transfer) stays available for
        # other real on-chain moves (e.g. a future withdrawal leg) even
        # though _execute_mint_comet no longer uses it for the IMC->USDT
        # leg — see _get_swap_rate_fn below for why.
        self._celo_transfer_fn = config.get("celo_transfer_fn", corridor_api.transfer_erc20)
        self.IMC_CELO_ADDRESS = config.get("imc_celo_address") or os.getenv(
            "IMC_CELO_ADDRESS", "0x766AA4F469A295330b10D150f842d71977D56dD6"
        )

        # Real Motor database handle (threaded through by
        # workers.corridor_worker._build_fsm) so _execute_mint_comet can
        # price IMC->USDT off the SAME treasury rate book retail swaps use
        # (routes.swap_engine.calculate_backend_rate), instead of Comet's
        # IMM/AMM rails. Confirmed live 2026-09-28: a real on-chain deposit
        # of IMC into Comet's own custodial wallet for externalUserId
        # "254" did NOT register in Comet's balance API or dashboard even
        # minutes later — Comet's ledger only credits balance for
        # transactions it processes itself (tokenize_airtime, its own
        # swaps), not arbitrary external transfers into an address it
        # controls. Rather than depend on an unconfirmed/undocumented
        # Comet deposit-recognition mechanism, the swap leg now stays
        # entirely internal: real IMC remains in the treasury wallet as
        # backing, and real USDT is paid out of the treasury's own
        # already-held USDT reserve at the rate book's price — the same
        # real-money bookkeeping retail swaps already do.
        self._db = config.get("db")

        async def _default_get_swap_rate(from_asset: str, to_asset: str) -> float:
            from routes.swap_engine import calculate_backend_rate
            if self._db is None:
                raise RuntimeError(
                    "No database handle configured (config['db']) — cannot "
                    "read the real treasury rate book."
                )
            return await calculate_backend_rate(from_asset, to_asset, self._db)

        self._get_swap_rate_fn = config.get("get_swap_rate_fn", _default_get_swap_rate)
        self._get_treasury_usdt_balance_fn = config.get(
            "get_treasury_usdt_balance_fn", _get_real_treasury_usdt_balance
        )
        self._get_treasury_imc_balance_fn = config.get(
            "get_treasury_imc_balance_fn", _get_real_treasury_imc_balance
        )
        # AWAITING_MANUAL_MINT state — populated by _execute_mint_comet when
        # Comet rejects a tenant-triggered tokenize, consumed/persisted by
        # _check_manual_mint and workers.corridor_worker. None outside that
        # window.
        self.pending_mint_expected_imc = config.get("resume_pending_mint_expected_imc")
        self.pending_mint_external_id = config.get("resume_pending_mint_external_id")
        self.pending_mint_treasury_imc_before = config.get("resume_pending_mint_treasury_imc_before")

        # AWAITING_MANUAL_TOPUP state — populated by _execute_procure when
        # the automated STK call fails or isn't confirmed in time, consumed
        # by _check_manual_topup. Same "wait for a real balance delta,
        # never trust a typed-in number" pattern as the mint pause above —
        # covers both a genuine STK failure and a deliberate manual top-up
        # (paying Paybill 5600000 directly, per impala_airtime.py's
        # topup_via_stk docstring).
        self.pending_topup_expected_kes = config.get("resume_pending_topup_expected_kes")
        self.pending_topup_txn_id = config.get("resume_pending_topup_txn_id")
        self.pending_topup_payout_balance_before = config.get("resume_pending_topup_payout_balance_before")
        self._find_opportunity_fn = config.get("find_opportunity_fn", find_best_open_opportunity)
        self._poll_seconds = config.get("poll_seconds", AWAITING_OPPORTUNITY_POLL_SECONDS)

        self._comet = config.get("comet_client", comet_client)
        self._get_discount_fn = config.get("get_discount_fn", impala_airtime.get_wholesale_discount_rate)
        self._payout_mobile_money_fn = config.get("payout_mobile_money_fn", daraja_service.payout_mobile_money)
        self._get_base_rate_fn = config.get("get_base_rate_fn", rate_feed.get_base_rate)
        self._get_payout_balance_fn = config.get("get_payout_balance_fn", impala_airtime.get_payout_balance)
        self._topup_via_stk_fn = config.get("topup_via_stk_fn", impala_airtime.topup_via_stk)

        # Real M-Pesa phone that approves each cycle's STK top-up prompt.
        # NOT necessarily the same number as PROCUREMENT_WALLETS (that was
        # the recipient for the old, wrong send_airtime-based PROCURE) —
        # confirm this is right for your real paying account before a live
        # run; it currently reuses PROCUREMENT_WALLETS as the only real
        # number already on file.
        self.STK_PAYING_PHONE = config.get("stk_paying_phone") or PROCUREMENT_WALLETS.get(self.NODE_PROCURE)

        # How long to poll the real payout balance for the STK top-up to
        # actually land before giving up — an STK push is a real,
        # asynchronous M-Pesa prompt (someone/something has to approve it),
        # not an instant API response.
        self.STK_POLL_ATTEMPTS = config.get("stk_poll_attempts", 12)
        self.STK_POLL_INTERVAL_SECONDS = config.get("stk_poll_interval_seconds", 5.0)

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
            
        elif self.state == FSMState.AWAITING_MANUAL_TOPUP:
            await self._check_manual_topup()

        elif self.state == FSMState.LIQUIDATE:
            await self._execute_liquidate()
            
        elif self.state == FSMState.MINT:
            if self.MINT_PROVIDER == "comet":
                await self._execute_mint_comet()
            else:
                await self._execute_mint()
            
        elif self.state == FSMState.AWAITING_MANUAL_MINT:
            await self._check_manual_mint()

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
        """Master Wallet -> Airtime Node float, via a real M-Pesa STK
        top-up (services.impala_airtime.topup_via_stk) — NOT send_airtime.

        Confirmed live 2026-09-25: send_airtime only ever debits an
        already-funded float 1:1 — it never captures the reseller
        discount. topup_via_stk is the real discount-capturing action (a
        real STK push to STK_PAYING_PHONE); a real 500 KES top-up returned
        525 KES of float, exactly amount * (1 + discount) — the LINEAR
        markup, not the inverse-discount formula (amount / (1 - discount))
        this method used before that real evidence existed.

        An STK push is a real, asynchronous prompt — this polls the real
        payout balance for it to actually land rather than trusting the
        topup response or the formula, since the endpoint's own response
        shape past "it was sent" isn't documented.
        """
        print(f"\n[CYCLE {self.current_cycle}] STATE 1: PROCURE via {self.NODE_PROCURE}")

        if not self.STK_PAYING_PHONE:
            self.halt_reason = f"No STK paying phone configured for {self.NODE_PROCURE}"
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

        # Live OCS discount confirmation (opt-in via use_live_discount) —
        # replaces trusting the static CORRIDORS[...]["discount"] config
        # value for this cycle. Fails closed: any error talking to the
        # provider, or a live rate below DISCOUNT_FLOOR, halts before a
        # single shilling is spent — never falls back to the static value
        # silently, since that would defeat the whole point of checking.
        if self.USE_LIVE_DISCOUNT:
            try:
                live_discount = await asyncio.to_thread(self._get_discount_fn)
            except Exception as e:
                self.halt_reason = f"Could not confirm live OCS discount rate — refusing to procure blind: {e}"
                print(f"  ↳ 🛑 {self.halt_reason}")
                self.state = FSMState.HALTED
                return
            try:
                check_discount_floor(live_discount, self.NODE_PROCURE, self.DISCOUNT_FLOOR)
            except RiskLimitExceeded as e:
                self.halt_reason = f"RISK LIMIT: {e}"
                print(f"  ↳ 🛑 {self.halt_reason}")
                self.state = FSMState.HALTED
                return
            print(f"  ↳ Live OCS discount confirmed: {live_discount:.4f} (floor {self.DISCOUNT_FLOOR:.4f})")
            self.DISCOUNT = live_discount

        # Real KES the STK prompt will actually charge — NOT inflated; the
        # discount arrives as extra float after the top-up lands, per the
        # linear markup confirmed live (see docstring), not before.
        amount_to_pay_kes = self.current_usd_principal * self.BASE_RATE
        # Deterministic, not random: same run_id + cycle always produces the
        # same txn_id, so a retry of this exact leg (e.g. after a crash)
        # reuses it instead of minting a fresh one — see run_id's docstring.
        txn_id = f"B2B-{self.run_id}-C{self.current_cycle}"

        try:
            balance_before = await asyncio.to_thread(self._get_payout_balance_fn)
            artm_before = balance_before["artm_balance"]
        except Exception as e:
            self.halt_reason = f"Could not read real payout balance before top-up — refusing to proceed blind: {e}"
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        try:
            # 🚀 EXTERNAL API CALL: real M-Pesa STK top-up (live — ImpalaPay)
            await asyncio.to_thread(
                self._topup_via_stk_fn,
                amount_kes=int(amount_to_pay_kes),
                paying_phone_number=self.STK_PAYING_PHONE,
            )
        except Exception as e:
            # Real, confirmed failure modes include a misconfigured
            # PROCUREMENT_WALLET_N2 (e.g. a non-Safaricom number — ImpalaPay's
            # STK rail only accepts Safaricom lines) as well as transient
            # network errors. Rather than halt outright, pause and watch the
            # real payout balance: if the top-up actually lands anyway (or
            # you pay Paybill 5600000 manually instead), the run picks it up
            # and continues on its own — see _check_manual_topup.
            print(f"  ↳ ⏸️  STK call failed ({e}). Pausing — top up manually "
                  f"(Paybill 5600000, account {self.NODE_PROCURE}) or fix the "
                  f"configured phone number; this run will detect the real "
                  f"balance increase and continue automatically.")
            self.pending_topup_expected_kes = amount_to_pay_kes
            self.pending_topup_txn_id = txn_id
            self.pending_topup_payout_balance_before = artm_before
            await self._transition_to(FSMState.AWAITING_MANUAL_TOPUP)
            return

        print(f"  ↳ 📲 STK prompt sent to {self.STK_PAYING_PHONE} for {amount_to_pay_kes:,.2f} KES — waiting for confirmation...")

        # STK is a real, asynchronous prompt (a human or an auto-approving
        # business account has to confirm it) — poll the real balance
        # rather than trusting an unconfirmed response shape or the linear-
        # markup formula. airtime_value_kes is the REAL observed delta.
        airtime_value_kes = None
        for attempt in range(self.STK_POLL_ATTEMPTS):
            await asyncio.sleep(self.STK_POLL_INTERVAL_SECONDS)
            try:
                balance_now = await asyncio.to_thread(self._get_payout_balance_fn)
            except Exception:
                continue
            delta = balance_now["artm_balance"] - artm_before
            if delta > 0:
                airtime_value_kes = delta
                print(f"  ↳ ✅ Top-up confirmed after {attempt + 1} poll(s): +{airtime_value_kes:,.2f} KES float")
                break

        if airtime_value_kes is None:
            # Not confirmed within the short poll window doesn't mean it
            # failed — the STK prompt may still be sitting unapproved on the
            # phone. Pause and keep watching rather than losing the run.
            print(f"  ↳ ⏸️  STK top-up not confirmed within "
                  f"{self.STK_POLL_ATTEMPTS * self.STK_POLL_INTERVAL_SECONDS:.0f}s. "
                  f"Pausing — approve the STK prompt (or top up manually); "
                  f"this run will detect the real balance increase and continue automatically.")
            self.pending_topup_expected_kes = amount_to_pay_kes
            self.pending_topup_txn_id = txn_id
            self.pending_topup_payout_balance_before = artm_before
            await self._transition_to(FSMState.AWAITING_MANUAL_TOPUP)
            return

        await self._finish_procure(airtime_value_kes, receipt_id=txn_id)

    async def _check_manual_topup(self):
        """Polls the real ImpalaPay payout balance while paused in
        AWAITING_MANUAL_TOPUP, waiting for a real increase — from the
        original STK prompt finally being approved, a retried STK call, or
        a manual Paybill payment. Uses the REAL observed delta, not
        pending_topup_expected_kes, so a manual top-up of a different
        amount than the corridor calculated is still recorded accurately."""
        try:
            balance_now = await asyncio.to_thread(self._get_payout_balance_fn)
        except Exception as e:
            print(f"  ↳ ⚠️  Could not re-check the real payout balance ({e}); retrying in {self._poll_seconds:.0f}s")
            await asyncio.sleep(self._poll_seconds)
            return

        delta = balance_now["artm_balance"] - (self.pending_topup_payout_balance_before or 0.0)
        if delta <= 0:
            print(f"[CYCLE {self.current_cycle}] STATE 1b: AWAITING MANUAL TOPUP — expecting ~{self.pending_topup_expected_kes:,.2f} KES, "
                  f"observed +{delta:,.2f} KES so far; re-checking in {self._poll_seconds:.0f}s")
            await asyncio.sleep(self._poll_seconds)
            return

        airtime_value_kes = delta
        receipt_id = self.pending_topup_txn_id
        print(f"  ↳ ✅ Real payout balance increase detected: +{airtime_value_kes:,.2f} KES "
              f"({self.pending_topup_payout_balance_before:,.2f} -> {balance_now['artm_balance']:,.2f}).")

        self.pending_topup_expected_kes = None
        self.pending_topup_txn_id = None
        self.pending_topup_payout_balance_before = None

        await self._finish_procure(airtime_value_kes, receipt_id=f"MANUAL-TOPUP-{receipt_id}")

    async def _finish_procure(self, airtime_value_kes: float, receipt_id: Optional[str]):
        """Shared tail end of both the direct-STK path (_execute_procure)
        and the manual-topup path (_check_manual_topup): record the real
        captured float and hand off to LIQUIDATE."""
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
        self.last_cycle_pnl_usd = profit
        self.cumulative_pnl_usd += profit
        self.current_usd_principal = new_usda_amount
        await self._transition_to(FSMState.ROLLOVER)

    # --- STATE 3 (Comet/IMC variant): MINT ---
    async def _execute_mint_comet(self):
        """Mobile Money Float -> IMC, via Comet's real airtime-tokenization
        endpoint (POST /api/v1/tokenize/airtime — docs.mamlakapsp.com/api/
        tokenization.html), NOT the IMM swap endpoint (that serves the
        separate, admin-priced OTC spread board). Comet's own invariant is
        "on-chain IMC supply = real airtime float remaining" — and its docs
        are explicit that Comet does NOT verify reserve availability
        itself, so check_airtime_backing (risk_engine.py) does that here
        against a real, live read of the ImpalaPay payout balance
        (services.impala_airtime.get_payout_balance) — the same "real
        external balance, not a ledger proxy" pattern _execute_mint already
        uses for the Cardano vault via _get_real_cardano_usda_balance.

        externalId is deterministic (run_id + cycle), matching the same
        idempotent-retry convention as _execute_procure's txn_id."""
        print(f"[CYCLE {self.current_cycle}] STATE 3 (Comet): MINT IMC")

        if self.COMET_USER_ID is None:
            self.halt_reason = "No Comet treasury user id configured (comet_user_id / COMET_TREASURY_USER_ID) — refusing to mint under an arbitrary account."
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        try:
            balance_data = await asyncio.to_thread(self._get_payout_balance_fn)
            real_artm_balance_kes = balance_data["artm_balance"]
        except Exception as e:
            self.halt_reason = f"Could not reach the real ImpalaPay payout balance — refusing to mint blind: {e}"
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        already_backed_imc = await self.ledger.get_vault_backed_total(self.NODE_MINT, "IMC")
        already_backed_kes_equiv = already_backed_imc * self.INTERNAL_FX_RATE

        try:
            check_airtime_backing(
                real_artm_balance_kes=real_artm_balance_kes,
                already_backed_kes_equiv=already_backed_kes_equiv,
                proposed_kes_equiv=self.current_kes_float,
                node_id=self.NODE_MINT,
            )
        except RiskLimitExceeded as e:
            self.halt_reason = f"RISK LIMIT: {e}"
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        # Same conversion the Cardano path uses for MINT (KES float at the
        # internal rate) — Comet's tokenize endpoint mints exactly the
        # amount we ask for, it doesn't compute this conversion for us.
        proposed_imc = self.current_kes_float / self.INTERNAL_FX_RATE

        external_id = f"MINT-{self.run_id}-C{self.current_cycle}"
        amount_base = self._comet.to_base_units(proposed_imc)
        try:
            result = await asyncio.to_thread(
                self._comet.tokenize_airtime,
                external_user_id=str(self.COMET_USER_ID),
                amount_base=amount_base,
                external_id=external_id,
                chain="celo",
            )
        except Exception as e:
            self.halt_reason = f"Comet tokenize_airtime failed: {e}"
            print(f"  ↳ ❌ {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        if result.get("status") != "success":
            # Confirmed live 2026-09-28: Comet rejects tenant-triggered
            # tokenize outright — "IMC is issued by the engine only —
            # tenants can't mint it, with their own relayer or otherwise."
            # This isn't a transient failure to halt on; it means minting
            # for this cycle has to happen manually, through Comet's own
            # dashboard, the same way the corridor's original 4.86036 IMC
            # was created. Rather than halt (or worse, trust a typed-in
            # "I minted N IMC" claim), the FSM pauses here and
            # _check_manual_mint polls the treasury's real on-chain IMC
            # balance until it actually rises — fully automatic once
            # someone mints via the dashboard, no manual confirm step, and
            # never fabricates an amount that wasn't really observed.
            try:
                treasury_imc_before = await asyncio.to_thread(self._get_treasury_imc_balance_fn)
            except Exception as e:
                self.halt_reason = f"Comet rejected the tokenize ({result.get('message')}) and could not read the real treasury IMC balance to wait on: {e}"
                print(f"  ↳ 🛑 {self.halt_reason}")
                self.state = FSMState.HALTED
                return
            self.pending_mint_expected_imc = proposed_imc
            self.pending_mint_external_id = external_id
            self.pending_mint_treasury_imc_before = treasury_imc_before
            print(f"  ↳ ⏸️  Comet rejected the tenant tokenize call ({result.get('message')}). "
                  f"Expecting ~{proposed_imc:,.4f} IMC — mint it manually via Comet's dashboard; "
                  f"this run will detect the real on-chain balance increase and continue automatically.")
            await self._transition_to(FSMState.AWAITING_MANUAL_MINT)
            return

        data = result.get("data", {})
        tx_hash = data.get("txHash")
        new_imc_amount = self._comet.from_base_units(data.get("amountBase", amount_base))
        profit = new_imc_amount - self.current_usd_principal
        print(f"  ↳ ✅ Tokenized {new_imc_amount:,.4f} IMC (backing verified pre-flight; tx {tx_hash}). "
              f"Profit: +{profit:,.4f}")

        # vault_backed=True here reflects OUR pre-flight check_airtime_backing
        # call above, not a flag Comet returned — the real endpoint's
        # response has no such field (see comet_client.tokenize_airtime).
        await self.ledger.append(LedgerEntry(
            from_node=self.NODE_LIQ, to_node=self.NODE_MINT, asset="IMC",
            amount=new_imc_amount, internal_usd_value=new_imc_amount,
            txn_type=TransactionType.MINT, cycle=self.current_cycle,
            external_ref=tx_hash or external_id, vault_backed=True,
        ))

        await self._finish_mint_with_internal_swap(new_imc_amount, profit)

    async def _check_manual_mint(self):
        """Polls the treasury's real on-chain IMC balance while paused in
        AWAITING_MANUAL_MINT, waiting for someone to mint the expected
        amount through Comet's dashboard directly (the only path Comet
        currently allows — see _execute_mint_comet's docstring). Uses the
        REAL observed delta as the minted amount, not
        pending_mint_expected_imc, so a partial or slightly different real
        mint is still recorded accurately rather than fabricated to match
        the estimate. A 1% tolerance below the expected amount accounts
        for float/rounding noise, not for a materially short deposit."""
        try:
            treasury_imc_now = await asyncio.to_thread(self._get_treasury_imc_balance_fn)
        except Exception as e:
            print(f"  ↳ ⚠️  Could not re-check the real treasury IMC balance ({e}); retrying in {self._poll_seconds:.0f}s")
            await asyncio.sleep(self._poll_seconds)
            return

        delta = treasury_imc_now - (self.pending_mint_treasury_imc_before or 0.0)
        expected = self.pending_mint_expected_imc or 0.0
        if delta < expected * 0.99:
            print(f"[CYCLE {self.current_cycle}] STATE 3b: AWAITING MANUAL MINT — expecting ~{expected:,.4f} IMC, "
                  f"observed +{delta:,.4f} IMC so far; re-checking in {self._poll_seconds:.0f}s")
            await asyncio.sleep(self._poll_seconds)
            return

        new_imc_amount = delta
        profit = new_imc_amount - self.current_usd_principal
        print(f"  ↳ ✅ Real on-chain mint detected: +{new_imc_amount:,.4f} IMC "
              f"(treasury balance {self.pending_mint_treasury_imc_before:,.4f} -> {treasury_imc_now:,.4f}). Profit: +{profit:,.4f}")

        await self.ledger.append(LedgerEntry(
            from_node=self.NODE_LIQ, to_node=self.NODE_MINT, asset="IMC",
            amount=new_imc_amount, internal_usd_value=new_imc_amount,
            txn_type=TransactionType.MINT, cycle=self.current_cycle,
            external_ref=f"MANUAL-MINT-{self.pending_mint_external_id}", vault_backed=True,
        ))

        self.pending_mint_expected_imc = None
        self.pending_mint_external_id = None
        self.pending_mint_treasury_imc_before = None

        await self._finish_mint_with_internal_swap(new_imc_amount, profit)

    async def _finish_mint_with_internal_swap(self, new_imc_amount: float, profit: float):
        """Shared tail end of both the direct-tokenize path
        (_execute_mint_comet) and the manual-mint path (_check_manual_mint):
        convert freshly-minted IMC to USDT internally, priced off the
        real treasury rate book retail swaps use, and hand off to ROLLOVER.
        """
        # Convert the freshly-minted IMC to USDT INTERNALLY, priced off the
        # same treasury rate book retail swaps use
        # (routes.swap_engine.calculate_backend_rate), rather than through
        # Comet's IMM or AMM rails. Confirmed live 2026-09-28: (1) Comet's
        # AMM pools for IMC are essentially unfunded (a real quote for 1
        # IMC returned ~0.00007-0.0007 USDT/USDC, not ~1), and (2) a real
        # on-chain deposit of IMC into Comet's own custodial wallet for
        # externalUserId "254" did NOT register in Comet's balance
        # API/dashboard even minutes later — Comet's ledger only credits
        # balance for transactions it processes itself (tokenize_airtime,
        # its own swaps), not an arbitrary external transfer into an
        # address it controls. Depending on that undocumented/unconfirmed
        # recognition path isn't something this corridor can build on.
        #
        # Instead: the real IMC stays in the treasury wallet as backing
        # (it already IS the proof-of-reserve for the airtime float, per
        # Comet's own tokenize_airtime invariant), and real USDT is paid
        # out of the treasury's own already-held USDT reserve at the rate
        # book's price. check_airtime_backing above already verified this
        # mint didn't over-issue against the real airtime float; the
        # balance check below verifies the treasury actually holds enough
        # real USDT before crediting any out — this never fabricates USDT
        # that doesn't exist.
        #
        # A failed check here still leaves the IMC mint on the ledger (it
        # already happened, real and irreversible) — it halts holding IMC
        # rather than crediting USDT the treasury can't back.
        try:
            rate = await self._get_swap_rate_fn("IMC", "USDT")
        except Exception as e:
            self.halt_reason = f"Could not price the internal IMC->USDT rate (IMC mint already settled): {e}"
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return
        if rate <= 0:
            self.halt_reason = f"Internal IMC->USDT rate lookup returned a non-positive rate ({rate}) (IMC mint already settled)."
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        quoted_usdt = new_imc_amount * rate

        # Slippage/price-sanity: IMC and USDT are both meant to track $1 —
        # this guards against a rate book entry that's simply mispriced
        # relative to the reference $1 peg (same check the Comet-IMM path
        # used, just against the internal rate instead).
        try:
            check_swap_slippage(new_imc_amount, quoted_usdt, expected_rate=1.0, pair_label="IMC/USDT")
        except RiskLimitExceeded as e:
            self.halt_reason = f"RISK LIMIT: {e} (IMC mint already settled)"
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        try:
            real_treasury_usdt = await asyncio.to_thread(self._get_treasury_usdt_balance_fn)
        except Exception as e:
            self.halt_reason = f"Could not verify the real treasury USDT balance — refusing to credit USDT blind (IMC mint already settled): {e}"
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return
        if real_treasury_usdt < quoted_usdt:
            self.halt_reason = (
                f"Insufficient real treasury USDT to honor this internal swap: "
                f"have {real_treasury_usdt:,.4f} USDT, need {quoted_usdt:,.4f} USDT (IMC mint already settled)."
            )
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        swap_external_id = f"MINT-SWAP-{self.run_id}-C{self.current_cycle}"
        usdt_received = quoted_usdt
        print(f"  ↳ 🔁 Internally converted {new_imc_amount:,.4f} IMC -> {usdt_received:,.4f} USDT "
              f"at rate book price {rate:.6f} (real treasury USDT reserve: {real_treasury_usdt:,.4f}).")

        await self.ledger.append(LedgerEntry(
            from_node=self.NODE_MINT, to_node=self.NODE_MINT, asset="USDT",
            amount=usdt_received, internal_usd_value=usdt_received,
            txn_type=TransactionType.TRANSFER, cycle=self.current_cycle,
            external_ref=f"INTERNAL-{swap_external_id}",
        ))

        self.last_cycle_pnl_usd = profit
        self.cumulative_pnl_usd += profit
        self.current_usd_principal = usdt_received
        await self._transition_to(FSMState.ROLLOVER)

    # --- STATE 4: THE ROLLOVER GATE ---
    async def _evaluate_rollover(self):
        """Checks cycle limits to prevent early blockchain gas fees.

        Below max_cycles, this no longer loops straight back to PROCURE —
        it hands off to AWAITING_OPPORTUNITY, which only starts the next
        cycle once the Decision Engine actually has an open, ranked
        opportunity to route through (see that method's docstring)."""
        at_max_cycles = self.current_cycle >= self.max_cycles

        # KES-exit path selection (spec Step 3, simplified — see
        # docstring on _execute_kes_exit): only evaluated when explicitly
        # enabled, so every existing corridor's behavior is unchanged.
        # internal_rate cancels out of the original "(usdt-internal) >
        # (cbk-internal)" comparison — this is directly "is cashing out
        # now worth more than the configured reinvestment threshold."
        if self.ENABLE_KES_EXIT_PATH and not at_max_cycles:
            try:
                cbk_rate = self._get_base_rate_fn(default=self.BASE_RATE)
            except Exception as e:
                print(f"  ↳ ⚠️  Could not read live base rate for KES-exit check, using configured BASE_RATE: {e}")
                cbk_rate = self.BASE_RATE
            kes_exit_value = self.current_usd_principal * cbk_rate
            if kes_exit_value >= self.REINVEST_THRESHOLD_KES > 0:
                print(f"[CYCLE {self.current_cycle}] STATE 4: KES exit ({kes_exit_value:,.2f} KES) "
                      f"clears reinvest threshold ({self.REINVEST_THRESHOLD_KES:,.2f}) -> EARLY EXIT")
                self.EXIT_PROVIDER = "kes"
                await self._transition_to(FSMState.CELO_EXIT)
                return

        if not at_max_cycles:
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
        """Dispatches on EXIT_PROVIDER first ("comet" / "kes" — the new
        Comet/IMC corridor variant), then falls through to whichever chain
        this run's config named as EXIT_CHAIN for the original "native"
        provider. Named _execute_exit rather than _execute_celo_exit to
        reflect that N9 is the whitepaper's "multi-chain router," not a
        Celo-only node — Celo just remains the only chain with a real,
        working settlement mechanism today."""
        if self.EXIT_PROVIDER == "comet":
            await self._execute_comet_exit()
        elif self.EXIT_PROVIDER == "kes":
            await self._execute_kes_exit()
        elif self.EXIT_CHAIN == "stellar":
            await self._execute_stellar_exit()
        else:
            await self._execute_celo_exit()

    async def _execute_comet_exit(self):
        """USDT -> USDC via Comet's real Celo AMM (POST /api/v1/swap/tokens
        — docs.mamlakapsp.com/api/amm.html), NOT the IMM swap endpoint.
        Source is USDT, not IMC: _execute_mint_comet already swaps freshly
        minted IMC to USDT immediately after minting, so USDT is what the
        corridor has been compounding since cycle 1 — there's no IMC left
        to swap at exit time. USDT/USDC is one of Comet's documented real
        pools. Settles into the same N9 node — no new wallet required.

        USDC, not cUSD, pending confirmation: Comet's documented AMM pools
        are USDT/USDC, USDT/IMC, USDC/IMC only — no cUSD pool is
        documented anywhere I've found. cUSD is Celo's own native
        Mento-stabilized asset, not necessarily reachable through Comet's
        AMM at all. Swap this to "USDT"/"cUSD" only once that's confirmed
        real (a Comet endpoint, or a direct Mento/Ubeswap integration this
        codebase doesn't have yet) — guessing here risks a swap call to a
        pool that silently doesn't exist.

        Liquidity/price check first via the real quote endpoint
        (GET /api/v1/swap/quote, already correct in comet_client.py before
        this session): read-only, so a bad quote halts before any real
        swap is attempted. The execute response's own shape (per the docs)
        doesn't echo an amountOut, so the pre-flight quote's amountOut is
        what gets recorded as the settled amount — reasonable for a
        same-block AMM swap, but it is an estimate, not a value Comet
        confirms back to us in the execute response itself.

        Reseed/sweep split: when FLOOR_KES > 0, a second swap sends just
        enough back to KES to refund tomorrow's procurement float
        (services.safaricom_daraja payout to NODE_LIQ's registered
        settlement number); everything above that is the real, realized
        profit this run made, and is what should move on to N8/cold
        storage next — that N8 credit is NOT wired in this pass; the swap
        result is logged as a TREASURY_SWEEP-eligible amount so a caller
        building that leg has a real, ledgered number to move rather than
        an estimate."""
        print(f"\n[FINAL] STATE 5 (Comet): USDT -> USDC EXIT")

        if self.COMET_USER_ID is None:
            self.halt_reason = "No Comet treasury user id configured (comet_user_id / COMET_TREASURY_USER_ID) — refusing to swap under an arbitrary account."
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        amount_base = self._comet.to_base_units(self.current_usd_principal)

        try:
            quote = await asyncio.to_thread(self._comet.get_amm_quote, "USDT", "USDC", amount_base)
        except Exception as e:
            self.halt_reason = f"Could not fetch Comet AMM quote before exit — refusing to swap blind: {e}"
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return
        if quote.get("status") != "success":
            self.halt_reason = f"Comet AMM quote failed: {quote.get('message')}"
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return
        quoted_usdc = self._comet.from_base_units(str(quote.get("data", {}).get("amountOut", 0)))
        if quoted_usdc <= 0:
            self.halt_reason = f"Comet AMM quote returned no usable amountOut: {quote.get('data')}"
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        # Same slippage/price-sanity guard as the mint-side IMC->USDT swap:
        # both legs of this pair are meant to track $1.
        try:
            check_swap_slippage(self.current_usd_principal, quoted_usdc, expected_rate=1.0, pair_label="USDT/USDC")
        except RiskLimitExceeded as e:
            self.halt_reason = f"RISK LIMIT: {e}"
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        external_id = f"EXIT-{self.run_id}-C{self.current_cycle}"
        try:
            result = await asyncio.to_thread(
                self._comet.execute_amm_swap,
                external_user_id=str(self.COMET_USER_ID),
                from_symbol="USDT", to_symbol="USDC", amount_in=amount_base,
            )
        except Exception as e:
            self.halt_reason = f"Comet AMM swap (USDT->USDC) failed: {e}"
            print(f"  ↳ ❌ {self.halt_reason}")
            self.state = FSMState.HALTED
            return
        if result.get("status") != "success":
            self.halt_reason = f"Comet rejected the USDT->USDC exit: {result.get('message')}"
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        data = result.get("data", {})
        tx_hash = data.get("txHash")
        usdc_received = quoted_usdc  # execute's own response doesn't echo amountOut per the docs — see docstring

        await self.ledger.append(LedgerEntry(
            from_node=self.NODE_MINT, to_node=self.NODE_EXIT, asset="USDC",
            amount=usdc_received, internal_usd_value=usdc_received,
            txn_type=TransactionType.CELO_EXIT, cycle=self.current_cycle,
            external_ref=tx_hash or external_id,
        ))
        print(f"  ↳ 🚀 Swapped to {usdc_received:,.4f} USDC inside Comet's custodial wallet (tx {tx_hash}).")

        # The swap alone only settles inside Comet's own custody for this
        # user — withdraw it to the real, self-custodied CELO_EXIT_ADDRESS
        # (same wallet the native _execute_celo_exit path already sends
        # to) so both corridor variants land in one wallet you control.
        if not self.COMET_EXIT_ADDRESS:
            self.halt_reason = "No CELO_EXIT_ADDRESS configured — USDC is settled inside Comet's custody but refusing to leave it stranded there uncontrolled; set CELO_EXIT_ADDRESS to withdraw it."
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        usdc_amount_base = self._comet.to_base_units(usdc_received)
        send_external_id = f"WITHDRAW-{self.run_id}-C{self.current_cycle}"
        try:
            send_result = await asyncio.to_thread(
                self._comet.send_asset,
                symbol="USDC",
                external_user_id=str(self.COMET_USER_ID),
                to=self.COMET_EXIT_ADDRESS,
                amount_base=usdc_amount_base,
            )
        except Exception as e:
            self.halt_reason = f"Withdrawal to CELO_EXIT_ADDRESS failed (USDC still sits in Comet custody, not lost): {e}"
            print(f"  ↳ ❌ {self.halt_reason}")
            self.state = FSMState.HALTED
            return
        if send_result.get("status") != "success":
            self.halt_reason = f"Comet rejected the withdrawal to CELO_EXIT_ADDRESS (USDC still sits in Comet custody, not lost): {send_result.get('message')}"
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        withdraw_tx_hash = send_result.get("data", {}).get("txHash")
        await self.ledger.append(LedgerEntry(
            from_node=self.NODE_EXIT, to_node=self.NODE_EXIT, asset="USDC",
            amount=usdc_received, internal_usd_value=usdc_received,
            txn_type=TransactionType.TRANSFER, cycle=self.current_cycle,
            external_ref=withdraw_tx_hash or send_external_id,
        ))
        print(f"  ↳ 🚀 SUCCESS: {usdc_received:,.4f} USDC withdrawn to {self.COMET_EXIT_ADDRESS} (tx {withdraw_tx_hash}).")

        sweepable = usdc_received
        if self.FLOOR_KES > 0:
            # USDC->KES crosses from an on-chain asset to fiat — that's not
            # an AMM pool pair (Comet's documented pools are all on-chain
            # token pairs: USDT/USDC, USDT/IMC, USDC/IMC), it's the IMM
            # swap endpoint's job (the same admin-priced KES<->IMC/asset
            # rail main.py's peg refresh already uses). Different call
            # convention: external_user_id is a string here, amount_in is
            # a plain float, not a base-unit string — do not mix these up
            # with the AMM calls above.
            reseed_kes = min(self.FLOOR_KES, usdc_received * self.BASE_RATE)
            reseed_usdc_amount = reseed_kes / self.BASE_RATE
            try:
                reseed_quote = await asyncio.to_thread(self._comet.get_imm_quote, "USDC", "KES", reseed_usdc_amount)
                reseed_result = await asyncio.to_thread(
                    self._comet.execute_imm_swap,
                    external_user_id=str(self.COMET_USER_ID),
                    chain="celo", base="USDC", quote="KES",
                    amount_in=reseed_usdc_amount,
                    external_id=f"RESEED-{self.run_id}-C{self.current_cycle}",
                )
            except Exception as e:
                print(f"  ↳ ⚠️  Reseed swap failed, leaving full amount as USDC for manual handling: {e}")
                reseed_result, reseed_quote = {"status": "error"}, {"status": "error"}

            if reseed_result.get("status") == "success" and reseed_quote.get("status") == "success":
                reseed_kes_actual = float(reseed_quote.get("data", {}).get("amountOut", 0))
                await self.ledger.append(LedgerEntry(
                    from_node=self.NODE_EXIT, to_node=self.NODE_LIQ, asset="KES",
                    amount=reseed_kes_actual, internal_usd_value=reseed_kes_actual / self.BASE_RATE,
                    txn_type=TransactionType.TRANSFER, cycle=self.current_cycle,
                    external_ref=reseed_result.get("data", {}).get("txHash"),
                ))
                sweepable = usdc_received - (reseed_kes_actual / self.BASE_RATE)
                print(f"  ↳ 🔁 Reseeded {reseed_kes_actual:,.2f} KES back to {self.NODE_LIQ} for tomorrow's PROCURE.")
                print(f"  ↳ 💰 Sweepable (not yet moved to N8/cold storage): {sweepable:,.4f} USDC-equivalent")

        self.exit_path_chosen = "comet"
        await self._transition_to(FSMState.COMPLETED)

    async def _execute_kes_exit(self):
        """IMC -> real KES cash payout via services.safaricom_daraja
        (payout_mobile_money — the same real B2C mechanism the earlier,
        replaced LIQUIDATE design used), instead of continuing to
        compound. Settlement target is NODE_LIQ's registered number in
        Brain_Engine.node_registry.LIQUIDATION_WALLETS — the same real
        settlement number the corridor already trusts for that node, not a
        new "operator payout" concept.

        Ends the run at COMPLETED regardless of current_cycle — this is
        the early-exit path _evaluate_rollover routes into when the KES
        valuation clears the reinvest threshold."""
        print(f"\n[FINAL] STATE 5 (KES exit): IMC -> real cash payout via {self.NODE_LIQ}")

        phone = LIQUIDATION_WALLETS.get(self.NODE_LIQ)
        if not phone:
            self.halt_reason = f"No settlement phone configured for {self.NODE_LIQ} — refusing to pay out blind."
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        try:
            cbk_rate = self._get_base_rate_fn(default=self.BASE_RATE)
        except Exception:
            cbk_rate = self.BASE_RATE
        kes_amount = self.current_usd_principal * cbk_rate

        try:
            balance_response = await asyncio.to_thread(self._get_merchant_balance_fn)
            check_liquidity_threshold(balance_response, kes_amount, self.NODE_LIQ)
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

        external_id = f"KESEXIT-{self.run_id}-C{self.current_cycle}"
        try:
            payout = await asyncio.to_thread(
                self._payout_mobile_money_fn,
                phone_number=phone, amount=int(kes_amount), transaction_id=external_id,
            )
        except Exception as e:
            self.halt_reason = f"KES payout failed: {e}"
            print(f"  ↳ ❌ {self.halt_reason}")
            self.state = FSMState.HALTED
            return
        if payout.get("status") != "success":
            self.halt_reason = f"KES payout rejected: {payout.get('message')}"
            print(f"  ↳ 🛑 {self.halt_reason}")
            self.state = FSMState.HALTED
            return

        await self.ledger.append(LedgerEntry(
            from_node=self.NODE_MINT, to_node=self.NODE_LIQ, asset="KES",
            amount=kes_amount, internal_usd_value=self.current_usd_principal,
            txn_type=TransactionType.TRANSFER, cycle=self.current_cycle,
            external_ref=payout.get("provider_id", external_id),
        ))
        print(f"  ↳ 🚀 SUCCESS: {kes_amount:,.2f} KES paid out to {phone} (real cash exit).")
        self.exit_path_chosen = "kes"
        await self._transition_to(FSMState.COMPLETED)

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

        # Gas-vs-margin: this is the one leg where WE pay real Celo gas
        # directly (_sync_celo_transfer signs and broadcasts from our own
        # treasury key) — refuse to broadcast if the cycle's cumulative
        # realized margin wouldn't meaningfully cover it. ESTIMATED_GAS_COST_USD
        # is a configurable placeholder (real Celo gas is typically well
        # under a cent, but this should be replaced with a live estimate —
        # celo_integrations._sync_celo_transfer already computes one
        # internally, it just isn't exposed to a pre-flight caller yet).
        try:
            check_gas_covers_margin(self.ESTIMATED_GAS_COST_USD, self.cumulative_pnl_usd)
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
        self.exit_path_chosen = "celo"
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