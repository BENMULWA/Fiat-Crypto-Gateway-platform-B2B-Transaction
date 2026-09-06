"""Backend-local shim for `cardano` package.

When the FastAPI app runs with CWD=`backend`, Python's import path doesn't include
the repository root, so create a local `cardano` package that forwards to the
actual implementation in `../cardano-USDA`.
"""

__all__ = ["wallet", "usda"]
