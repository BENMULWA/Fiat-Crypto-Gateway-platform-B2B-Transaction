import importlib.util
import sys
from pathlib import Path

# Resolve the real implementation file by going up to the repository root
# Check where we are safely
current_dir = Path(__file__).resolve().parent

# If we are inside backend/cardano, go up to find cardano-USDA
if current_dir.parents[0].name == "backend":
    # Local setup: go up past cardano/ and backend/ to the project root
    _src = current_dir.parents[1] / "cardano-USDA" / "wallet.py"
else:
    # Render setup: cardano-USDA is likely right next to your running folder
    _src = current_dir.parents[0] / "cardano-USDA" / "wallet.py"

spec = importlib.util.spec_from_file_location("cardano.wallet", str(_src))
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)
sys.modules["cardano.wallet"] = _mod

# Re-export common names
CardanoWallet = getattr(_mod, "CardanoWallet")
get_or_create_wallet_index = getattr(_mod, "get_or_create_wallet_index")
