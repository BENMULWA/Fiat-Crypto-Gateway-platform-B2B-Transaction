"""
Canonical Node Registry (N1-N10) per the Mamlaka IMM Strategic Whitepaper.

Single source of truth for node identity so the FSM, the ledger, the Risk
Engine (future work), and the frontend all reference the same ten IDs
instead of ad hoc strings.

daily_limit_usd / min_reserve_ratio are placeholders, not policy: the Risk
Engine that will actually enforce them hasn't been built yet.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from Brain_Engine.cache import memory_cache

# This module reads PROCUREMENT_WALLET_N2 directly from the environment —
# load .env here too (idempotent, same pattern as impala_airtime.py and
# safaricom_daraja.py) so that value resolves correctly even if this module
# is ever imported before whatever else in the app happens to load .env
# first, instead of silently falling back to "" and halting every corridor.
load_dotenv()


@dataclass(frozen=True)
class Node:
    id: str
    label: str
    asset_type: str  # AIRTIME | MOBILE_MONEY | STABLECOIN | TREASURY | CELO | POOL
    category: str    # PROCURE | LIQUIDATE | MINT | TREASURY | EXIT | POOL
    daily_limit_usd: float = 0.0
    min_reserve_ratio: float = 0.0
    min_balance: float = 0.0        # hard floor on this node's real ledger balance; 0 = unset
    exposure_cap_usd: float = 0.0   # cap on this node's net outstanding (synthetic) balance; 0 = unset
    live: bool = False  # True only once a real external integration exists for this node

# all Nodes available
NODES = {
    "N1": Node("N1", "Telkom KE", "AIRTIME", "PROCURE", daily_limit_usd=500.0, live=False),
    "N2": Node("N2", "Airtel KE", "AIRTIME", "PROCURE", daily_limit_usd=500.0, live=True),
    "N3": Node("N3", "Safaricom", "AIRTIME", "PROCURE", daily_limit_usd=500.0, live=False),
    "N4": Node("N4", "T-Kash Agent", "MOBILE_MONEY", "LIQUIDATE", daily_limit_usd=1000.0, live=False),
    # Sized to the real Mam-laka merchant KES balance (600, confirmed live
    # via get_merchant_balance()) rather than a round number: KES 600 /
    # 129.50 baseline ~= $4.63, capped at $4.50 to leave a buffer below the
    # account's actual balance. This has reverted to a placeholder 1000.0
    # more than once this session — re-verify the real balance before
    # raising it, don't just restore a bigger number from habit.
    "N5": Node("N5", "Mpesa", "MOBILE_MONEY", "LIQUIDATE", daily_limit_usd=4.5, live=True),
    "N6": Node("N6", "Airtel Money", "MOBILE_MONEY", "LIQUIDATE", daily_limit_usd=4.5, live=True),
    "N7": Node("N7", "USDA Vault", "STABLECOIN", "MINT"),
    "N8": Node("N8", "IMM Treasury", "TREASURY", "TREASURY", min_reserve_ratio=0.20),
    "N9": Node("N9", "Celo Exit", "CELO", "EXIT", live=True),
    "N10": Node("N10", "Virt Card", "POOL", "POOL"),
}

# The asset symbol each node's real ledger balance is tracked under —
# ImmutableLedger.get_balance(node_id, asset) needs both, and this is the
# one place that mapping is written down instead of re-guessed at every
# call site. A node with no entry here has never been credited/debited by
# any code path yet (N8, N10) — there's no balance to look up.
NODE_LEDGER_ASSET = {
    "N1": "AIRTIME_KES",
    "N2": "AIRTIME_KES",
    "N3": "AIRTIME_KES",
    "N4": "KES",
    "N5": "KES",
    "N6": "KES",
    "N7": "USDA",
    "N9": "USDC",
}

# Only N2 (Airtel, via the Mam-laka/Impala gateway's known Airtel-prefixed
# wallet) has a real procurement wallet wired today. Adding a new procure
# node means adding its real wallet/phone here first. Read from env, not
# hardcoded, same convention as the Mam-laka/Celo/Cardano credentials this
# module's callers already source from .env.
PROCUREMENT_WALLETS = {
    "N2": os.getenv("PROCUREMENT_WALLET_N2", ""),
}

# LIQUIDATE-side settlement: the real buyer's phone number a corridor
# collects (STK push) real KES FROM, via Mam-laka's mobile-money rail
# (same gateway — mamlakapsp — the retail (Jasiri) ramp flow already uses).
# That buyer purchases the airtime procured earlier in the cycle — this is
# the real-world airtime-to-fiat buy-back, not a payout to them. Empty
# until real buyer numbers are supplied — HFTCorridorFSM._execute_liquidate
# halts cleanly on a missing entry, same as PROCUREMENT_WALLETS above.
LIQUIDATION_WALLETS = {
    "N5": "0725603575",  # : real M-Pesa settlement number
    "N6": "0783253036",  # : real Airtel Money settlement number
}

# mobileMoneySP value Mam-laka's API expects for each liquidation node.
# Passed explicitly rather than inferred from the phone number — inferring
# it is exactly the bug in the retail ramp flow that silently routes
# Airtel Money requests to M-Pesa.
LIQUIDATION_PROVIDERS = {
    "N5": "M-Pesa",
    "N6": "Airtel",
}

# Candidate rollover strategies. Keyed separately from NODES because a
# corridor is a procure+liquidate node *pair* plus its own discount/fx_edge
# economics, not a node property. IDs match the "telkom_5x"/"airtel_5x"
# scheme already used by market_maker.py's /opportunities and treasury.py's
# /corridor/execute-hft — this is the single source of truth both the
# autonomous DecisionEngine (Brain_Engine/bot.py) and the dealer's manual
# "Deploy" button read from, so a switch flipped here actually gates both.
CORRIDORS = {
    "airtel_5x": {
        "name": "AIRTEL LIVE", "node_procure": "N2", "node_liquidate": "N5",
        "discount": 0.06, "fx_edge": 0.0,
    },
    "telkom_5x": {
        "name": "TELKOM", "node_procure": "N1", "node_liquidate": "N4",
        "discount": 0.10, "fx_edge": 0.05,
    },
}


def get_node(node_id: str) -> Node:
    if node_id not in NODES:
        raise ValueError(f"Unknown node id: {node_id!r}. Valid nodes: {sorted(NODES)}")
    return NODES[node_id]


def require_live(node_id: str) -> Node:
    """Raises if this node has no real external integration yet — call this
    before routing real capital through it instead of silently simulating."""
    node = get_node(node_id)
    if not node.live:
        raise ValueError(
            f"Node {node_id} ({node.label}) has no live integration yet — "
            "refusing to simulate a real-money corridor through it."
        )
    return node


# --- Admin runtime switches -------------------------------------------------
# `live` above is static (does a real integration exist at all); these are
# the dynamic admin on/off switches on top of that — e.g. pull N1 out of
# service for maintenance without touching the registry, or park the whole
# TELKOM strategy while keeping AIRTEL_LIVE running. Defaults to enabled so
# existing behavior is unchanged until an admin flips one off.

def is_node_enabled(node_id: str) -> bool:
    get_node(node_id)  # raises on unknown id
    value = memory_cache.get(f"imm:node:{node_id}:enabled")
    return True if value is None else bool(value)


def set_node_enabled(node_id: str, enabled: bool) -> None:
    get_node(node_id)
    memory_cache.set(f"imm:node:{node_id}:enabled", enabled)


def is_corridor_enabled(corridor_id: str) -> bool:
    if corridor_id not in CORRIDORS:
        raise ValueError(f"Unknown corridor id: {corridor_id!r}. Valid corridors: {sorted(CORRIDORS)}")
    value = memory_cache.get(f"imm:corridor:{corridor_id}:enabled")
    return True if value is None else bool(value)


def set_corridor_enabled(corridor_id: str, enabled: bool) -> None:
    if corridor_id not in CORRIDORS:
        raise ValueError(f"Unknown corridor id: {corridor_id!r}. Valid corridors: {sorted(CORRIDORS)}")
    memory_cache.set(f"imm:corridor:{corridor_id}:enabled", enabled)


def corridor_eligible(corridor_id: str) -> bool:
    """A corridor may run only if it's itself enabled AND every node it
    routes through is both live (real integration exists) and admin-enabled."""
    corridor = CORRIDORS[corridor_id]
    if not is_corridor_enabled(corridor_id):
        return False
    for node_id in (corridor["node_procure"], corridor["node_liquidate"]):
        node = get_node(node_id)
        if not node.live or not is_node_enabled(node_id):
            return False
    return True
