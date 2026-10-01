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


def check_airtime_backing(real_artm_balance_kes: float, already_backed_kes_equiv: float,
                           proposed_kes_equiv: float, node_id: str) -> None:
    """Comet's POST /api/v1/tokenize/airtime does NOT verify reserve
    availability itself — docs.mamlakapsp.com/api/tokenization.html is
    explicit: "The caller (typically app-core-backend) must verify the
    ImpalaPay float balance before calling this endpoint." This is that
    verification: real, LIVE airtime inventory balance
    (services.impala_airtime.get_payout_balance — same real external read
    _get_real_cardano_usda_balance is for the Cardano/USDA path) vs. real
    cumulative IMC already minted-and-backed (ImmutableLedger.
    get_vault_backed_total, the exact method _execute_mint already uses
    for the Cardano vault, reused here rather than duplicated) plus this
    cycle's proposed mint. All three arguments are already resolved by the
    caller — deliberately not async / not ledger-aware, so this is a pure
    comparison, same style as check_discount_floor."""
    projected = already_backed_kes_equiv + proposed_kes_equiv
    if projected > real_artm_balance_kes:
        raise RiskLimitExceeded(
            f"{node_id}: real ImpalaPay airtime balance is {real_artm_balance_kes:,.2f} KES, "
            f"{already_backed_kes_equiv:,.2f} KES-equivalent already minted as IMC, this mint needs "
            f"{proposed_kes_equiv:,.2f} more (would total {projected:,.2f}) — "
            f"refusing to mint IMC unbacked by real airtime inventory."
        )


def check_swap_slippage(amount_in: float, amount_out: float, expected_rate: float,
                         pair_label: str, max_deviation_pct: float = 0.02) -> None:
    """Raises RiskLimitExceeded if a quoted AMM swap's implied rate
    (amount_out / amount_in) deviates from `expected_rate` by more than
    max_deviation_pct. Called on the QUOTE, before every real AMM
    execute — this is the only slippage/price-sanity protection available
    here: Comet's own AMM execute endpoint (POST /api/v1/swap/tokens)
    takes no caller-supplied amountOutMin/destMin (the docs only mention
    that control existing on Comet's Stellar path, not documented for
    Celo), so a bad or manipulated pool price can't be capped by us at
    execution time — it can only be refused before we ever call execute.

    expected_rate is the reference "global market" price for the pair:
    1.0 for stablecoin<->stablecoin legs (USDT/USDC, IMC/USDT — both
    sides meant to track $1), or the fixed base rate for any leg crossing
    into/out of KES. A limit of 0.0 means "no reference configured yet" —
    skipped, same convention as every other check here."""
    if expected_rate <= 0 or amount_in <= 0:
        return
    implied_rate = amount_out / amount_in
    deviation = abs(implied_rate - expected_rate) / expected_rate
    if deviation > max_deviation_pct:
        raise RiskLimitExceeded(
            f"{pair_label}: quoted rate {implied_rate:.6f} deviates {deviation*100:.2f}% from the "
            f"expected {expected_rate:.6f} (limit {max_deviation_pct*100:.2f}%) — refusing to swap "
            f"at a price this far from the reference market rate."
        )


def check_gas_covers_margin(estimated_gas_cost_usd: float, realized_margin_usd: float,
                             min_coverage_ratio: float = 3.0) -> None:
    """Raises RiskLimitExceeded if a real on-chain settlement's gas cost
    would consume too much of the margin it's meant to realize —
    protects against broadcasting a real, gas-paying transaction whose
    cost eats most or all of the profit it's settling. min_coverage_ratio
    is how many times bigger the margin must be than the gas cost (default
    3x: gas may take at most ~33% of the margin) — deliberately
    conservative given how thin single-cycle margins are at pilot scale."""
    if estimated_gas_cost_usd <= 0:
        return
    if realized_margin_usd < estimated_gas_cost_usd * min_coverage_ratio:
        raise RiskLimitExceeded(
            f"Estimated gas cost ${estimated_gas_cost_usd:.4f} is too large relative to the "
            f"${realized_margin_usd:.4f} margin being settled (need margin >= "
            f"{min_coverage_ratio}x gas) — refusing to broadcast a transaction that would eat "
            f"most or all of the profit it's meant to realize."
        )


def check_discount_floor(discount_rate: float, node_id: str, floor: float = 0.035) -> None:
    """Raises RiskLimitExceeded if the live wholesale discount a provider
    just quoted (services.impala_airtime.get_wholesale_discount_rate) has
    fallen below `floor` — the corridor's economics assume at least this
    much margin exists to capture; below it, procuring anyway risks a
    cycle that costs more than it earns once the internal-rate spread and
    gas are netted out. Called after the live quote comes back, before any
    money is spent — same fail-closed convention as every other check
    here."""
    if discount_rate < floor:
        raise RiskLimitExceeded(
            f"{node_id}: live wholesale discount {discount_rate:.4f} is below the "
            f"configured floor {floor:.4f} — refusing to procure at an uneconomic rate."
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
