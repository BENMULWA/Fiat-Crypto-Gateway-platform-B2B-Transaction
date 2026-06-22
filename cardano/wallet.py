import importlib.util
import sys
from pathlib import Path

# Resolve the real implementation file by going up to the repository root
# (backend/cardano/wallet.py -> parents[2] == project root)
repo_root = Path(__file__).resolve().parents[2]
# Try a few candidate locations (some environments / checkouts may differ)
candidates = [
	repo_root / "cardano-USDA" / "wallet.py",
	repo_root / "backend" / "cardano-USDA" / "wallet.py",
	repo_root / "cardano_usda" / "wallet.py",
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
		f"{checked}\n\nPlease ensure `cardano-USDA/wallet.py` exists at the project root or adjust the path."
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
