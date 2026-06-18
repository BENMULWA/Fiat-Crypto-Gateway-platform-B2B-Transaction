import importlib.util
import sys
from pathlib import Path

# Fix the path layout to look inside the correct backend folder level
current_dir = Path(__file__).resolve().parent
_src = current_dir.parent / "cardano-USDA" / "usda.py"

spec = importlib.util.spec_from_file_location("cardano.usda", str(_src))
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)
sys.modules["cardano.usda"] = _mod

# Re-export module symbols
globals().update({k: getattr(_mod, k) for k in dir(_mod) if not k.startswith("__")})
