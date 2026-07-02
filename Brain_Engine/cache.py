import time
from typing import Dict, Any

class StateCache:
    """
    Simulates a Redis In-Memory Cache for sub-10ms response times.
    (Section 6 of the White Paper)
    """
    def __init__(self):
        self._store: Dict[str, Any] = {
            "system:kill_switch": False,
            "rates:binance_usdt_kes": 130.50,
            "rates:cbk_kes_usd": 129.80,
            "spread:usda_kes:bid": 128.00,
            "spread:usda_kes:ask": 132.00,
            "corridor:target_roi": 1.25, # Target 25% minimum ROI across 5 cycles
        }

    def get(self, key: str) -> Any:
        return self._store.get(key)

    def set(self, key: str, value: Any):
        self._store[key] = value

# Global Singleton instance
memory_cache = StateCache()