"""
Risk Engine: turns Node.daily_limit_usd, Node.min_balance,
Node.exposure_cap_usd, and the whitepaper's "Liquidity Thresholds" check
from documented placeholders into actually-enforced pre-trade checks.

Node.min_reserve_ratio is still NOT enforced here — the IMM Treasury node
(N8) has no wired cashflow anywhere in the current FSM (nothing ever
credits it), so there is no real balance yet to compute a reserve ratio
against. Wire that check in once N8 actually receives funds; enforcing a
ratio against an always-zero balance would just block every corridor
unconditionally.

Every check here shares one convention: a limit of 0.0 means "nobody has
configured this yet," so the check is skipped rather than blocking
everything through a node nobody has sized a policy for.
"""

from datetime import datetime, timezone

from Brain_Engine.node_registry import get_node, NODE_LEDGER_ASSET


class RiskLimitExceeded(Exception):
    """Raised when a proposed leg would breach an enforced risk limit.
    Callers should treat this the same as any other pre-flight guard in the
    FSM: halt the corridor cleanly, don't let it reach the external call."""


async def check_daily_procurement_limit(ledger, node_id: str, proposed_usd: float, txn_type) -> None:
    """Raises RiskLimitExceeded if procuring another `proposed_usd` through
    node_id today would push its cumulative PROCURE volume past
    daily_limit_usd. A limit of 0.0 means "no cap configured" for that node
    (see node_registry's placeholder note) — skip the check rather than
    block every leg through nodes nobody has sized a limit for yet."""
    node = get_node(node_id)
    if node.daily_limit_usd <= 0:
        return

    already_today = await ledger.get_node_volume_today(node_id, txn_type)
    projected = already_today + proposed_usd
    if projected > node.daily_limit_usd:
        raise RiskLimitExceeded(
            f"{node_id} ({node.label}) daily procurement limit exceeded: "
            f"already procured ${already_today:,.2f} today, this leg "
            f"(${proposed_usd:,.2f}) would bring it to ${projected:,.2f}, "
            f"cap is ${node.daily_limit_usd:,.2f}."
        )


def check_liquidity_threshold(balance_response: dict, required_kes: float, node_id: str) -> None:
    """Raises RiskLimitExceeded unless the Mam-laka merchant account
    (services.safaricom_daraja.DarajaService.get_merchant_balance()'s
    return value, passed in already-fetched) has enough kesBalance to cover
    a payout of `required_kes`.

    Fails closed on anything unexpected — a balance call that errored, or a
    response missing kesBalance — is treated as "can't confirm sufficient
    funds," not as "assume it's fine." A payout is real, external, and
    irreversible; the only safe default here is to block it."""
    if balance_response.get("status") != "success":
        raise RiskLimitExceeded(
            f"{node_id}: could not verify Mam-laka merchant balance before payout "
            f"({balance_response.get('message', 'unknown error')}) — refusing to pay out blind."
        )

    kes_balance = balance_response.get("data", {}).get("kesBalance")
    if kes_balance is None:
        raise RiskLimitExceeded(
            f"{node_id}: Mam-laka balance response had no kesBalance field — refusing to pay out blind."
        )

    if float(kes_balance) < required_kes:
        raise RiskLimitExceeded(
            f"{node_id}: Mam-laka merchant KES balance too low for this payout. "
            f"Have KES {float(kes_balance):,.2f}, need KES {required_kes:,.2f}."
        )


async def check_min_balance(ledger, node_id: str, debit_amount: float) -> None:
    """Raises RiskLimitExceeded if debiting `debit_amount` from node_id's
    real ledger balance would take it below Node.min_balance — the same
    "refuse to execute on an assumed balance" guarantee a corridor-hop
    engine gives every debit, applied here to the two spots in the current
    FSM that actually debit a node's ledger balance (N7 before PROCURE and
    before CELO_EXIT). Skips nodes with no ledger asset mapped yet, and
    nodes with no floor configured (min_balance <= 0)."""
    node = get_node(node_id)
    if node.min_balance <= 0:
        return

    asset = NODE_LEDGER_ASSET.get(node_id)
    if not asset:
        return  # nothing has ever credited/debited this node — no real balance to check yet

    current_balance = await ledger.get_balance(node_id, asset)
    projected = current_balance - debit_amount
    if projected < node.min_balance:
        raise RiskLimitExceeded(
            f"{node_id} ({node.label}) has {current_balance:,.4f} {asset}, needs "
            f"{debit_amount:,.4f} while keeping its {node.min_balance:,.4f} floor — "
            f"refusing to execute on an assumed balance."
        )


async def check_exposure_cap(ledger, node_id: str, additional_amount: float) -> None:
    """Raises RiskLimitExceeded if crediting `additional_amount` more to
    node_id would push its net ledger balance (credits − debits — i.e. its
    outstanding, potentially-synthetic position) past exposure_cap_usd.

    This is the lightweight version of Comet's exposure cap: it doesn't
    require a real funded vault to exist first (see the Ledger
    Reconciliation report's "vault-backed vs synthetic mint" gap) — it
    just bounds how much ledger-only value a node is allowed to accumulate
    before someone has to look at it. Skipped when exposure_cap_usd is
    unconfigured (0.0) or the node has no ledger asset mapped yet."""
    node = get_node(node_id)
    if node.exposure_cap_usd <= 0:
        return

    asset = NODE_LEDGER_ASSET.get(node_id)
    if not asset:
        return

    current_balance = await ledger.get_balance(node_id, asset)
    projected = current_balance + additional_amount
    if projected > node.exposure_cap_usd:
        raise RiskLimitExceeded(
            f"{node_id} ({node.label}) exposure cap exceeded: currently holds "
            f"{current_balance:,.4f} {asset}, this leg (+{additional_amount:,.4f}) "
            f"would bring it to {projected:,.4f}, cap is {node.exposure_cap_usd:,.4f}."
        )


# How long an admin-set rate is trusted before it's treated as stale — same
# 60-minute default as the Comet IMM reference doc. A safety net for a rate
# nobody's revisited, not a substitute for keeping it current.
RATE_STALE_AFTER_SECONDS = 3600


def rate_age_seconds(updated_at_iso: str | None) -> float | None:
    """Seconds since an ISO-8601 updatedAt timestamp, or None if there
    isn't one yet (the rate has never been explicitly set)."""
    if not updated_at_iso:
        return None
    try:
        updated_at = datetime.fromisoformat(updated_at_iso)
    except ValueError:
        return None
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated_at).total_seconds()


def is_rate_stale(updated_at_iso: str | None, max_age_seconds: float = RATE_STALE_AFTER_SECONDS) -> bool:
    """True if the rate is older than max_age_seconds — or has never been
    explicitly set at all. An unset rate is exactly as untrustworthy as a
    stale one; both mean "nobody has confirmed this number recently.\""""
    age = rate_age_seconds(updated_at_iso)
    return age is None or age > max_age_seconds
