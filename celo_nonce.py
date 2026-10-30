"""Collision-free nonces for transactions signed with the shared Celo treasury key.

Four independent code paths sign with the SAME treasury key -- customer
withdrawals (routes/valora.py), swap settlement (routes/swap_engine.py), deposit
gas top-ups (workers/celo_deposit_watcher.py) and the corridor exit
(Brain_Engine/celo_integrations.py). Each used `get_transaction_count(addr)`,
which counts only MINED transactions. Two of them signing within the same block
interval received the same nonce: one transaction replaced the other or was
rejected ("nonce too low" / "replacement underpriced"), surfacing as a random
failed withdrawal or a swap whose on-chain leg silently never happened.

reserve_treasury_nonce():
  * asks the node for the PENDING count (includes unmined txs the node knows about);
  * never returns a nonce at or below one this process already handed out in the
    last NONCE_CACHE_SECONDS, which covers the window before the node has seen
    our previous broadcast;
  * is serialised by a lock, so concurrent threads/requests get distinct values.

The cache expires so that a nonce reserved but never broadcast (build/sign error)
cannot leave a permanent gap: after the window the node's pending count is used
again and the gap is filled.

Limit: this protects ONE process. Running several API instances against the same
treasury key needs a shared signer (a queue/service) instead.
"""
import threading
import time

NONCE_CACHE_SECONDS = 120

_lock = threading.Lock()
_last: dict[str, tuple[int, float]] = {}


def reserve_treasury_nonce(w3, address: str) -> int:
    with _lock:
        chain_pending = w3.eth.get_transaction_count(address, "pending")
        nonce = chain_pending
        cached = _last.get(address)
        if cached and (time.monotonic() - cached[1]) < NONCE_CACHE_SECONDS:
            nonce = max(chain_pending, cached[0] + 1)
        _last[address] = (nonce, time.monotonic())
        return nonce
