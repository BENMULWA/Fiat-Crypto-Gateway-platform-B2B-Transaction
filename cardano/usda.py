import importlib.util
import sys
from pathlib import Path

# Resolve the real implementation file by going up to the repository root
# (backend/cardano/usda.py -> parents[2] == project root)
_src = Path(__file__).resolve().parents[2] / "cardano-USDA" / "usda.py"
spec = importlib.util.spec_from_file_location("cardano.usda", str(_src))
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)
sys.modules["cardano.usda"] = _mod

# Re-export module symbols
globals().update({k: getattr(_mod, k) for k in dir(_mod) if not k.startswith("__")})
