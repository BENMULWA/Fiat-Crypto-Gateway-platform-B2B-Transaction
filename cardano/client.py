import importlib.util
import sys
from pathlib import Path

# Fix the path layout to look inside the correct backend folder level
current_dir = Path(__file__).resolve().parent
_src = current_dir.parent / "cardano-USDA" / "client.py"

spec = importlib.util.spec_from_file_location("cardano.client", str(_src))
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)
sys.modules["cardano.client"] = _mod

# Re-export common symbols
globals().update({k: getattr(_mod, k) for k in dir(_mod) if not k.startswith("__")})
