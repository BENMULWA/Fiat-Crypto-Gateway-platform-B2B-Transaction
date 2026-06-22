import importlib.util
import sys
from pathlib import Path

# Get the immediate parent folder of this file (cardano/)
current_dir = Path(__file__).resolve().parent

# Dynamic path structural checks for both Local Dev and Cloud environments
candidates = [
    # 1. Local environment setup (project/backend/cardano-USDA/wallet.py)
    current_dir.parent / "cardano-USDA" / "wallet.py",
    
    # 2. Render environment setup (project/cardano-USDA/wallet.py)
    current_dir.parent.parent / "cardano-USDA" / "wallet.py",
    
    # 3. Fallbacks matching your previous definitions
    current_dir.parent.parent / "backend" / "cardano-USDA" / "wallet.py",
    current_dir.parent / "cardano_usda" / "wallet.py"
]

_src = None
for p in candidates:
    p = p.resolve()
    if p.exists():
        _src = p
        break

if _src is None:
    checked = "\n".join(str(p) for p in candidates)
    raise RuntimeError(
        "cardano implementation file not found. Checked the following paths:\n"
        f"{checked}\n\nPlease ensure `cardano-USDA/wallet.py` exists or adjust the path lookup structure."
    )

spec = importlib.util.spec_from_file_location("cardano.wallet", str(_src))
_mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(_mod)
except FileNotFoundError as exc:
    raise RuntimeError(f"Failed to load cardano implementation from {_src}: {exc}") from exc
sys.modules["cardano.wallet"] = _mod

# Re-export common names
CardanoWallet = getattr(_mod, "CardanoWallet")
get_or_create_wallet_index = getattr(_mod, "get_or_create_wallet_index")
