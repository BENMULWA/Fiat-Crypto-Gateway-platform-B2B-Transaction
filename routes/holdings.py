import asyncio
import os
import time
from datetime import datetime

from fastapi import APIRouter, Depends

from database import get_db
from routes.auth import get_current_user_with_role, is_admin_role
from services import holdings as hold

router = APIRouter(prefix="/api/imm/holdings", tags=["Holdings"])

CACHE_SECONDS = 20
_cache: dict = {"at": 0.0, "result": None}
# The AMM wallet Comet shows as "Pairs are created and funded from": the wallet that holds our liquidity. Override with AMM_LP_WALLET.
DEFAULT_AMM_WALLET = "0x76270853b3B914AD69D6406E0574aAAC983fe140"


async def _roster(db) -> list[dict]:
    from routes.valora import get_treasury_address
    from services.comet_client import CometClient

    wallets = [{"role": "master", "label": "Master wallet", "address": get_treasury_address(), "always": True}]
    uid = os.getenv("COMET_TREASURY_USER_ID")
    if uid:
        try:
            w = await asyncio.to_thread(CometClient().get_or_create_wallet, str(uid))
            addr = (w.get("data") or {}).get("address")
            if addr:
                wallets.append({"role": "engine", "label": "Engine wallet (Comet)", "address": addr, "always": False})
        except Exception:
            pass
    exit_addr = os.getenv("CELO_EXIT_ADDRESS", "").strip().strip("'\"")
    if exit_addr:
        wallets.append({"role": "exit", "label": "Exit wallet (yours)", "address": exit_addr, "always": True})
    wallets.append({"role": "amm", "label": "AMM wallet (Comet liquidity)", "address": os.getenv("AMM_LP_WALLET", DEFAULT_AMM_WALLET), "always": False})
    try:
        b = await db["imc_bookings"].find_one({"treasury": {"$exists": True, "$ne": None}})
        if b and b.get("treasury"):
            wallets.append({"role": "cometTreasury", "label": "Comet treasury", "address": b["treasury"], "always": False})
    except Exception:
        pass
    seen, out = set(), []
    for w in wallets:                                   # one entry per address, first role wins
        key = str(w["address"]).lower()
        if key not in seen:
            seen.add(key)
            out.append(w)
    return out


def _deps(wallets: list[dict]) -> hold.Deps:
    from routes.valora import ASSET_CONTRACTS, w3
    from routes.treasury import _read_stellar_treasury_balance_sync
    from routes.cardano import get_master_wallet_balance
    from Brain_Engine.state_engine import _real_celo_token_balance_of
    from services.comet_client import CometClient
    from services.impala_airtime import impala_airtime
    from services.safaricom_daraja import DarajaService

    comet, daraja = CometClient(), DarajaService()
    pair_abi = [{"constant": True, "inputs": [{"name": "o", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
                {"constant": True, "inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}]

    def lp(pair: str, wallet: str):
        c = w3.eth.contract(address=w3.to_checksum_address(pair), abi=pair_abi)
        return (c.functions.balanceOf(w3.to_checksum_address(wallet)).call() / 1e18, c.functions.totalSupply().call() / 1e18)

    def usda():
        r = asyncio.run(get_master_wallet_balance())
        return float(r.get("usda", 0.0) or 0.0) if isinstance(r, dict) and r.get("status") == "success" else None

    def xlm():
        r = _read_stellar_treasury_balance_sync()
        return float(r.get("xlm", 0.0) or 0.0) if r.get("status") == "success" else None

    amm = next((w["address"] for w in wallets if w["role"] == "amm"), None)
    return hold.Deps(
        wallets=wallets, contracts={s: ASSET_CONTRACTS[s] for s in hold.TOKENS if s in ASSET_CONTRACTS},
        token_balance_fn=_real_celo_token_balance_of, celo_balance_fn=lambda a: float(w3.from_wei(w3.eth.get_balance(w3.to_checksum_address(a)), "ether")),
        pools_fn=comet.list_amm_pools, lp_fn=lp, lp_wallet=amm,
        float_fn=lambda: impala_airtime.get_payout_balance()["artm_balance"],
        gateway_fn=daraja.get_merchant_balance, gateway_is_sandbox="sandbox" in str(daraja.base_url).lower(),
        usda_fn=usda, xlm_fn=xlm)


@router.get("")
async def holdings(refresh: bool = False, db=Depends(get_db), current_user=Depends(get_current_user_with_role)):
    """What the IMM really holds, read live: airtime float, Celo wallets, pool liquidity, and the gateway (flagged when sandbox).
    Operators see wallet addresses; everyone else gets the same figures without them. Cached for a few seconds."""
    admin = is_admin_role(current_user.get("role"))
    now = time.time()
    if refresh and admin or not _cache["result"] or now - _cache["at"] > CACHE_SECONDS:
        wallets = await _roster(db)
        _cache["result"] = await hold.gather(_deps(wallets))
        _cache["at"] = time.time()
        _cache["asOf"] = datetime.utcnow().isoformat() + "Z"
    result = _cache["result"] if admin else hold.strip_addresses(_cache["result"])
    return {"status": "success", "asOf": _cache.get("asOf"), **result}
