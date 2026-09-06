"""Persistent background watcher + provisioning for Stellar per-user deposit
accounts. Mirrors workers/celo_deposit_watcher.py and
workers/cardano_deposit_watcher.py in shape, but Stellar has two mechanics
neither of those chains have:

1. A derived keypair isn't a usable account until it exists on-chain. A brand
   new Stellar address must be funded with a CreateAccount operation before
   it can receive anything at all — see `provision_stellar_account_sync`,
   called lazily the first time a user's deposit address is requested.
2. Holding a non-native asset (USDC) requires a funded trustline — created in
   the same provisioning transaction so a freshly provisioned address can
   receive USDC immediately, not just XLM.

Only USDC is supported as a Stellar stablecoin here — Tether does not
officially issue USDT on Stellar (confirmed against tether.to's own
supported-protocols list on 2026-09-04), so trusting a "USDT" asset on
Stellar would mean trusting an unverified third-party token with that ticker.

Detection works like the Cardano watcher: no event log on Stellar either, so
this diffs each address's current USDC balance (read from Horizon) against a
stored watermark.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime
from decimal import Decimal

from pymongo.errors import DuplicateKeyError
from stellar_sdk import Asset, Keypair, Network, Server, TransactionBuilder
from stellar_sdk.exceptions import NotFoundError

from broadcast import broadcast_manager
from notifications import notify_user
from stellar_child_wallet import derive_stellar_child_account, get_stellar_treasury_keypair

logger = logging.getLogger("stellar.deposit_watcher")

HORIZON_URL = os.getenv("STELLAR_HORIZON_URL", "https://horizon.stellar.org")
POLL_INTERVAL_SECONDS = int(os.getenv("STELLAR_WATCHER_INTERVAL_SECONDS", "5"))

# Circle's official USDC issuer on Stellar mainnet — verified directly against
# circle.com on 2026-09-04. Do not change without re-verifying against an
# authoritative source; trusting the wrong issuer means crediting users for a
# worthless lookalike asset.
USDC_ISSUER = os.getenv("STELLAR_USDC_ISSUER", "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN")
USDC_ASSET = Asset("USDC", USDC_ISSUER)

# Starting balance for a newly provisioned child account. The hard minimum for
# one account + one trustline is (2 + 1 subentries) * 0.5 base reserve = 1.5
# XLM — trimmed down from an earlier 3 XLM default (2026-09-04) to reduce the
# real, ongoing per-user cost this locks up; 1.7 leaves a small buffer above
# the bare minimum for fees without over-provisioning.
CHILD_STARTING_BALANCE_XLM = Decimal(os.getenv("STELLAR_CHILD_STARTING_BALANCE_XLM", "1.7"))
# An account must keep at least this much XLM to remain valid with one
# trustline (2 + 1 subentries) * 0.5 base reserve = 1.5 XLM. Leave a small
# safety margin above the bare minimum so a fee deduction never invalidates it.
MIN_RESERVE_XLM = Decimal(os.getenv("STELLAR_MIN_RESERVE_XLM", "1.6"))


def _get_server() -> Server:
    return Server(horizon_url=HORIZON_URL)


def provision_stellar_account_sync(index: int) -> str:
    """Ensure the child account for this index exists on-chain and trusts
    USDC. Safe to call repeatedly — a no-op once already provisioned. Returns
    the child's public address."""
    server = _get_server()
    child = derive_stellar_child_account(index)
    treasury = get_stellar_treasury_keypair()

    try:
        child_account = server.load_account(child.public_key)
        account_exists = True
    except NotFoundError:
        account_exists = False

    if not account_exists:
        treasury_account = server.load_account(treasury.public_key)
        tx = (
            TransactionBuilder(
                source_account=treasury_account,
                network_passphrase=Network.PUBLIC_NETWORK_PASSPHRASE,
                base_fee=server.fetch_base_fee(),
            )
            .append_create_account_op(destination=child.public_key, starting_balance=str(CHILD_STARTING_BALANCE_XLM))
            .append_change_trust_op(asset=USDC_ASSET, source=child.public_key)
            .set_timeout(60)
            .build()
        )
        tx.sign(treasury)
        tx.sign(child)
        try:
            server.submit_transaction(tx)
        except Exception:
            # Horizon can report a submission error to this client even when the
            # transaction actually landed on-chain (observed live on 2026-09-04:
            # two submissions raised errors here, but both accounts were
            # confirmed created on-chain moments later). Check reality before
            # surfacing a scary "unavailable" error to a user whose account may
            # already exist.
            try:
                server.load_account(child.public_key)
                logger.warning(
                    "submit_transaction raised for index=%s but the account exists on-chain — treating as success.",
                    index,
                )
            except NotFoundError:
                raise
        logger.info("Provisioned new Stellar account for index=%s: %s", index, child.public_key)
        return child.public_key

    # Account exists already — check whether it's missing the USDC trustline
    # (e.g. provisioned before USDC support existed) and add it if so.
    # server.load_account() only returns sequence-number info for building
    # transactions, not balances — the raw Horizon accounts() call is what
    # actually carries the balances/trustlines list.
    raw_account = server.accounts().account_id(child.public_key).call()
    balances = raw_account.get("balances", [])
    has_trustline = any(
        b.get("asset_code") == "USDC" and b.get("asset_issuer") == USDC_ISSUER
        for b in balances
    )

    if not has_trustline:
        xlm_balance = Decimal(next((b["balance"] for b in balances if b.get("asset_type") == "native"), "0"))
        if xlm_balance < MIN_RESERVE_XLM:
            _topup_child_xlm_sync(server, treasury, child.public_key, MIN_RESERVE_XLM - xlm_balance + Decimal("0.5"))
            child_account = server.load_account(child.public_key)

        tx = (
            TransactionBuilder(
                source_account=child_account,
                network_passphrase=Network.PUBLIC_NETWORK_PASSPHRASE,
                base_fee=server.fetch_base_fee(),
            )
            .append_change_trust_op(asset=USDC_ASSET)
            .set_timeout(60)
            .build()
        )
        tx.sign(child)
        server.submit_transaction(tx)
        logger.info("Added USDC trustline to existing Stellar account for index=%s", index)

    return child.public_key


def is_stellar_account_provisioned_sync(index: int) -> bool:
    """Read-only check — costs nothing, no XLM spent. Used so the deposit page
    can show the (freely derived) address immediately without triggering real
    on-chain provisioning just because someone looked at the page; the actual
    account+trustline creation only happens when the user explicitly confirms
    they want to deposit (see provision_stellar_account_sync)."""
    server = _get_server()
    child = derive_stellar_child_account(index)
    try:
        raw_account = server.accounts().account_id(child.public_key).call()
    except NotFoundError:
        return False
    balances = raw_account.get("balances", [])
    return any(b.get("asset_code") == "USDC" and b.get("asset_issuer") == USDC_ISSUER for b in balances)


def _topup_child_xlm_sync(server: Server, treasury, child_address: str, amount_xlm: Decimal):
    treasury_account = server.load_account(treasury.public_key)
    tx = (
        TransactionBuilder(
            source_account=treasury_account,
            network_passphrase=Network.PUBLIC_NETWORK_PASSPHRASE,
            base_fee=server.fetch_base_fee(),
        )
        .append_payment_op(destination=child_address, asset=Asset.native(), amount=str(amount_xlm))
        .set_timeout(60)
        .build()
    )
    tx.sign(treasury)
    server.submit_transaction(tx)


def _get_balances_sync(server: Server, address: str) -> dict:
    try:
        data = server.accounts().account_id(address).call()
    except NotFoundError:
        return {"xlm": Decimal("0"), "usdc": Decimal("0")}

    xlm = Decimal("0")
    usdc = Decimal("0")
    for b in data.get("balances", []):
        if b.get("asset_type") == "native":
            xlm = Decimal(b["balance"])
        elif b.get("asset_code") == "USDC" and b.get("asset_issuer") == USDC_ISSUER:
            usdc = Decimal(b["balance"])
    return {"xlm": xlm, "usdc": usdc}


def _sweep_deposit_sync(index: int) -> dict:
    """Sweep the child's full USDC balance to treasury. XLM above the minimum
    reserve is left in place (it's what pays for the account's own future
    trustline/fee needs) rather than swept, unlike Celo/Cardano's gas model —
    Stellar's reserve is locked collateral, not a spendable balance to reclaim
    per deposit."""
    server = _get_server()
    child = derive_stellar_child_account(index)
    treasury = get_stellar_treasury_keypair()

    balances = _get_balances_sync(server, child.public_key)
    result = {"sweepTxHash": None, "sweptUsdc": 0.0}
    if balances["usdc"] <= 0:
        return result

    child_account = server.load_account(child.public_key)
    tx = (
        TransactionBuilder(
            source_account=child_account,
            network_passphrase=Network.PUBLIC_NETWORK_PASSPHRASE,
            base_fee=server.fetch_base_fee(),
        )
        .append_payment_op(destination=treasury.public_key, asset=USDC_ASSET, amount=str(balances["usdc"]))
        .set_timeout(60)
        .build()
    )
    tx.sign(child)
    response = server.submit_transaction(tx)
    result["sweepTxHash"] = response.get("hash")
    result["sweptUsdc"] = float(balances["usdc"])
    return result


def send_stellar_withdrawal_sync(destination: str, amount: Decimal) -> str:
    """Send USDC from the treasury to an external Stellar address — the real
    on-chain counterpart to a user-facing withdrawal. Unlike sweeping a child
    deposit account, the destination here is arbitrary and outside our
    control, so it must already exist on-chain and already trust USDC —
    Stellar cannot receive a non-native asset otherwise. Raises on any
    failure; the caller is responsible for refunding the user's internal
    balance if this raises."""
    server = _get_server()
    treasury = get_stellar_treasury_keypair()

    try:
        Keypair.from_public_key(destination)
    except Exception as exc:
        raise ValueError(f"Invalid Stellar destination address: {destination}") from exc

    try:
        dest_account = server.accounts().account_id(destination).call()
    except NotFoundError as exc:
        raise ValueError("Destination Stellar account does not exist on-chain yet.") from exc

    has_trustline = any(
        b.get("asset_code") == "USDC" and b.get("asset_issuer") == USDC_ISSUER
        for b in dest_account.get("balances", [])
    )
    if not has_trustline:
        raise ValueError("Destination Stellar account has not set up a USDC trustline.")

    treasury_account = server.load_account(treasury.public_key)
    tx = (
        TransactionBuilder(
            source_account=treasury_account,
            network_passphrase=Network.PUBLIC_NETWORK_PASSPHRASE,
            base_fee=server.fetch_base_fee(),
        )
        .append_payment_op(destination=destination, asset=USDC_ASSET, amount=str(amount))
        .set_timeout(60)
        .build()
    )
    tx.sign(treasury)

    try:
        response = server.submit_transaction(tx)
    except Exception:
        # Mirrors provision_stellar_account_sync: Horizon can report a client
        # error even when the transaction landed on-chain. We can't verify a
        # payment landed as cheaply as we can verify an account exists, so
        # this still raises — the caller refunds the user's balance — but the
        # exception is worth distinguishing in logs from a genuine failure.
        raise
    return response.get("hash")


async def _process_scan(db):
    index_docs = await db["stellar_child_wallet_indexes"].find({}, {"_id": 1, "userId": 1, "index": 1}).to_list(length=20000)
    if not index_docs:
        return

    server = _get_server()

    for doc in index_docs:
        wallet_index = doc["index"]
        user_id = doc["userId"]

        try:
            child = await asyncio.to_thread(derive_stellar_child_account, wallet_index)
            balances = await asyncio.to_thread(_get_balances_sync, server, child.public_key)
        except Exception:
            logger.exception("Failed to check balance for wallet index=%s", wallet_index)
            continue

        usdc_balance = balances["usdc"]
        if usdc_balance <= 0:
            continue

        watermark_doc = await db["stellar_deposit_watermarks"].find_one({"_id": child.public_key})
        last_usdc = Decimal(str((watermark_doc or {}).get("lastUsdc", "0")))

        if usdc_balance <= last_usdc:
            continue

        delta = usdc_balance - last_usdc

        claim = await db["stellar_deposit_watermarks"].find_one_and_update(
            {"_id": child.public_key, "lastUsdc": (watermark_doc or {}).get("lastUsdc", "0")},
            {"$set": {"lastUsdc": str(usdc_balance), "updatedAt": datetime.utcnow()}},
            upsert=not watermark_doc,
        )
        if watermark_doc and not claim:
            continue  # another cycle already claimed this increase

        delta_float = float(delta)
        await db["retail_wallets"].update_one(
            {"userId": user_id},
            {"$inc": {"USDC": delta_float}},
            upsert=True,
        )

        now = datetime.utcnow()
        await db["ramp_entries"].insert_one({
            "_id": f"TRADE_{uuid.uuid4().hex[:8].upper()}",
            "direction": "on",
            "channel": "Stellar Auto-Detect",
            "fromAsset": "USDC",
            "toAsset": "USDC",
            "fromAmount": delta_float,
            "toAmount": delta_float,
            "status": "completed",
            "userId": user_id,
            "depositAddress": child.public_key,
            "createdAt": now,
            "date": now.strftime("%b %d, %Y"),
            "timeAgo": "Just now",
        })

        try:
            await broadcast_manager.send_user(str(user_id), {
                "type": "deposit_credited",
                "userId": str(user_id),
                "asset": "USDC",
                "amount": delta_float,
                "network": "stellar",
            })
        except Exception:
            logger.debug("Broadcast failed for user %s (non-fatal)", user_id)

        await notify_user(
            db, user_id, "deposit", "success",
            "Deposit received",
            f"{delta_float:g} USDC arrived on Stellar and was credited to your account.",
            extra={"asset": "USDC", "amount": delta_float},
        )

        try:
            sweep_result = await asyncio.to_thread(_sweep_deposit_sync, wallet_index)
            logger.info("Swept wallet index=%s: %s", wallet_index, sweep_result)
        except Exception:
            logger.exception("Sweep failed for wallet index=%s — funds remain safely on the deposit address", wallet_index)


async def stellar_deposit_watcher_loop(db, stop_event: asyncio.Event | None = None):
    stop_event = stop_event or asyncio.Event()
    logger.info("Starting Stellar deposit watcher (interval=%ss)", POLL_INTERVAL_SECONDS)
    try:
        while not stop_event.is_set():
            try:
                await _process_scan(db)
            except Exception:
                logger.exception("Unexpected error in Stellar deposit watcher iteration")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                continue
    finally:
        logger.info("Stellar deposit watcher stopping")
