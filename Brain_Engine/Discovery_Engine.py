from pydantic import BaseModel
from typing import Dict, Any

class NodeMetrics(BaseModel):
    velocity: float
    demand: float
    liquidity: float
    risk: float

class IMMDiscoveryEngine:
    """
    Implements the Intelligence Layer and Price Discovery logic from the 
    Mamlaka Strategic Whitepaper.
    """
    def __init__(self):
        # Baseline Market Anchor (e.g., CBK Official Rate)
        self.baseline_rate_kes_usd = 129.50
        
    def calculate_liquidity_score(self, metrics: NodeMetrics) -> float:
        """Calculates
         the health of an individual Node (N1 - N10) to determine
        if it is safe to route capital through it.
        """
        score = (
            (metrics.velocity * 0.35) +
            (metrics.demand * 0.25) +
            (metrics.liquidity * 0.25) -
            (metrics.risk * 0.15)
        )
        return max(0.0, min(100.0, score))

    def project_corridor_yield(
        self,
        discount_rate: float,
        fx_edge_pct: float,
        cycles: int = 5,
        slippage_pct: float = 0.0,
        fee_pct: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Projects the exact mathematical multiplier for a corridor based on
        the UI state parameters (e.g., 6% Discount, 0% FX Edge).

        slippage_pct/fee_pct default to 0.0 (today's unchanged behavior for
        every existing caller) but let a caller that DOES have a real AMM
        quote or swap-fee figure fold it into the multiplier instead of
        pricing the corridor as if every leg fills at its quoted rate with
        zero cost — see risk_engine.check_swap_slippage, which enforces a
        ceiling on this same number but never fed it back into the yield
        projection until now.
        """
        # Step 1: Procurement Edge (The Yield)
        procurement_multiplier = 1 / (1 - discount_rate)

        # Step 2: Internal Mint Edge (The Spread)
        mint_multiplier = 1 / (1 - fx_edge_pct)

        # Step 3: Execution cost drag (slippage + swap fee) — this is what
        # eff_rate_e = quoted_rate_e * (1 - fee_e - slippage_e) collapses to
        # when there's one dominant on-chain leg per cycle rather than a
        # separately-priced hop per edge.
        execution_multiplier = 1 - slippage_pct - fee_pct

        # Step 4: Combined Single-Cycle Multiplier (the G-factor for one pass)
        cycle_multiplier = procurement_multiplier * mint_multiplier * execution_multiplier

        # Step 5a: Compound mode — reinvest principal + profit into every
        # subsequent cycle (this is what HFTCorridorFSM does today:
        # current_usd_principal carries the prior cycle's mint forward).
        # Grows faster but concentrates all N cycles' exposure on one
        # position — a bad fill on cycle 4 or 5 erases more of the run.
        total_compounded_multiplier = cycle_multiplier ** cycles

        # Step 5b: Skim mode — re-risk only the original principal each
        # cycle; profit is banked out of the corridor after every pass
        # instead of rolled forward. Linear, not exponential, but caps
        # at-risk capital at the starting principal for the whole day
        # rather than letting it grow with every win.
        total_skim_multiplier = 1 + cycles * (cycle_multiplier - 1)

        return {
            "single_cycle_multiplier": round(cycle_multiplier, 4),
            "total_5x_multiplier": round(total_compounded_multiplier, 4),
            "projected_profit_pct": round((total_compounded_multiplier - 1) * 100, 2),
            "skim_mode_multiplier": round(total_skim_multiplier, 4),
            "skim_mode_profit_pct": round((total_skim_multiplier - 1) * 100, 2),
        }