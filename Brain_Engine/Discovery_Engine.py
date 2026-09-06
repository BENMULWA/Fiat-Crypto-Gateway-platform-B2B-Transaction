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

    def project_corridor_yield(self, discount_rate: float, fx_edge_pct: float, cycles: int = 5) -> Dict[str, Any]:
        """
        Projects the exact mathematical multiplier for a corridor based on 
        the UI state parameters (e.g., 6% Discount, 0% FX Edge).
        """
        # Step 1: Procurement Edge (The Yield)
        procurement_multiplier = 1 / (1 - discount_rate)
        
        # Step 2: Internal Mint Edge (The Spread)
        mint_multiplier = 1 / (1 - fx_edge_pct)
        
        # Step 3: Combined Single-Cycle Multiplier
        cycle_multiplier = procurement_multiplier * mint_multiplier
        
        # Step 4: Compounding over N cycles (e.g., 5-cycle Rollover)
        total_compounded_multiplier = cycle_multiplier ** cycles
        
        return {
            "single_cycle_multiplier": round(cycle_multiplier, 4),
            "total_5x_multiplier": round(total_compounded_multiplier, 4),
            "projected_profit_pct": round((total_compounded_multiplier - 1) * 100, 2)
        }