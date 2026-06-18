import importlib.util
import sys
from pathlib import Path

# Resolve the real implementation file by going up to the repository root
_src = Path(__file__).resolve().parents[2] / "cardano-USDA" / "client.py"
spec = importlib.util.spec_from_file_location("cardano.client", str(_src))
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)
sys.modules["cardano.client"] = _mod

# Re-export common symbols
globals().update({k: getattr(_mod, k) for k in dir(_mod) if not k.startswith("__")})
