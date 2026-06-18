import importlib.util
import sys
from pathlib import Path

# Resolve the real implementation file by going up to the repository root
# (backend/cardano/wallet.py -> parents[2] == project root)
_src = Path(__file__).resolve().parents[2] / "cardano-USDA" / "wallet.py"
spec = importlib.util.spec_from_file_location("cardano.wallet", str(_src))
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)
sys.modules["cardano.wallet"] = _mod

# Re-export common names
CardanoWallet = getattr(_mod, "CardanoWallet")
get_or_create_wallet_index = getattr(_mod, "get_or_create_wallet_index")
