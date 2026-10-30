import importlib.util
import sys
from pathlib import Path

_source = Path(__file__).resolve().parent.parent / "cardano-USDA" / "usda.py"
if not _source.is_file():
    raise ImportError(f"Cardano USDA implementation not found: {_source}")

_impl_name = "_cardano_usda_impl"
_spec = importlib.util.spec_from_file_location(_impl_name, _source)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Could not load Cardano USDA implementation: {_source}")

_impl = importlib.util.module_from_spec(_spec)
sys.modules[_impl_name] = _impl
_spec.loader.exec_module(_impl)

for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)