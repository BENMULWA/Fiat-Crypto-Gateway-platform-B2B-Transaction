import time
from datetime import datetime
from typing import Optional

import requests

from config import settings


class ZigramError(Exception):
    pass


def is_zigram_configured() -> bool:
    """True once real ZIGRAM credentials are set (ZIGRAM_USERNAME,
    ZIGRAM_USER_SECRET, ZIGRAM_PROJECT_ID). Callers use this to skip
    screening entirely while credentials aren't provisioned yet (e.g. local/
    QA testing before the vendor account is live) instead of every ramp/swap
    piling up in pending_compliance_review via the fail-closed ZigramError
    path. The moment real credentials are set in the environment, this flips
    to True and screening resumes automatically -- no code change needed."""
    return bool(
        settings.zigram_username
        and settings.zigram_user_secret
        and settings.zigram_project_id is not None
    )


class ZigramClient:
    """
    Client for ZIGRAM's Transact Comply transaction monitoring API
    (https://qa.transactcomply.com/api-docs).

    NOTE on Content-Type: the docs list application/x-www-form-urlencoded for
    GetTokenEncryption but show a JSON example body. This implementation sends
    JSON to match the example. If ZIGRAM's sandbox rejects it with a 415/400,
    switch _get_token()'s request to `data=` (form-encoded) instead of `json=`.

    NOTE on Monitoring_Status: ZIGRAM's docs only document "Flag" as an example
    value and never publish the full enum (their Data Field Details page has no
    such table). Do not add heuristics here that treat unrecognized values as
    "clear" -- confirm the exact clear/pass status string with ZIGRAM support
    and add it to ZIGRAM_CLEAR_STATUSES.
    """

    def __init__(self):
        self.base_url = settings.zigram_base_url.rstrip("/") + "/"
        self.username = settings.zigram_username
        self.user_secret = settings.zigram_user_secret
        self.project_id = settings.zigram_project_id
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at:
            return self._token

        if not self.username or not self.user_secret:
            raise ZigramError("ZIGRAM_USERNAME / ZIGRAM_USER_SECRET are not configured")

        url = f"{self.base_url}GetTokenEncryption"
        payload = {
            "user_name": self.username,
            "user_secret": self.user_secret,
            "grant_type": "client_credentials",
        }
        try:
            resp = requests.post(url, json=payload, timeout=15)
        except Exception as e:
            raise ZigramError(f"Failed to reach ZIGRAM auth endpoint: {e}")

        if resp.status_code != 200:
            raise ZigramError(f"ZIGRAM auth failed: {resp.status_code} {resp.text}")

        data = resp.json()
        token_block = data.get("token") or {}
        access_token = token_block.get("access_token")
        if not access_token:
            raise ZigramError(f"ZIGRAM auth response missing access_token: {data}")

        expires_in = int(token_block.get("expires_in", 30))
        # refresh a little early to avoid racing a 30s expiry
        self._token = access_token
        self._token_expires_at = now + max(expires_in - 5, 1)
        return self._token

    def submit_transaction(
        self,
        *,
        customer_id: str,
        transaction_id: str,
        amount: float,
        currency: str,
        mode: str,
        transaction_type: str,
        transaction_status: str,
        channel: str,
        transaction_date: Optional[datetime] = None,
    ) -> dict:
        """
        Screens a single transaction. Returns a dict:
            {
                "is_success": bool,
                "monitoring_status": str | None,
                "case_display_id": str | None,
                "master_case_display_id": str | None,
                "raw": <full parsed response>,
            }
        Raises ZigramError on network/auth failure or a non-2xx response.
        """
        if self.project_id is None:
            raise ZigramError("ZIGRAM_PROJECT_ID is not configured")

        token = self._get_token()
        transaction_date = transaction_date or datetime.utcnow()

        payload = {
            "Project_Id": self.project_id,
            "Customer_Id": customer_id,
            "Transaction_Id": transaction_id,
            "Transaction_Date": transaction_date.strftime("%d-%b-%Y").upper(),
            "TransactionAdditionalDetailsJSON": {
                "Transaction_Time": transaction_date.strftime("%H:%M:%S.%f")[:-3],
                "Transaction_Amount": str(amount),
                "Transaction_Mode": mode,
                "Transaction_Currency": currency,
                "Transaction_Type": transaction_type,
                "Transaction_Status": transaction_status,
                "Transaction_Channel": channel,
            },
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        url = f"{self.base_url}TransactionProcessorAPI"
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=20)
        except Exception as e:
            raise ZigramError(f"Failed to reach ZIGRAM TransactionProcessorAPI: {e}")

        if resp.status_code != 200:
            raise ZigramError(f"ZIGRAM transaction screening failed: {resp.status_code} {resp.text}")

        data = resp.json()
        case = data.get("CaseAlertResponseModel") or {}
        # docs show this as a single object in one example and a list in the webhook
        # sample -- handle both shapes defensively.
        if isinstance(case, list):
            case = case[0] if case else {}

        return {
            "is_success": bool(data.get("Is_Success", False)),
            "monitoring_status": case.get("Monitoring_Status"),
            "case_display_id": case.get("Case_Display_Id"),
            "master_case_display_id": case.get("MasterCase_Display_Id") or case.get("Master_Case_Display_Id"),
            "transaction_batch_id": case.get("Transaction_Batch_Id"),
            "raw": data,
        }


def is_clear_status(monitoring_status: Optional[str]) -> bool:
    """
    True only if ZIGRAM's Monitoring_Status has been explicitly whitelisted via
    ZIGRAM_CLEAR_STATUSES. Empty/unset config means nothing auto-clears -- every
    transaction holds for manual review until that env var is set. See the
    ZigramClient docstring for why this doesn't guess.
    """
    if not monitoring_status:
        return False
    return monitoring_status in settings.zigram_clear_statuses


_FIAT_CODES = {"KES", "USD", "EUR", "GBP", "UGX", "TZS", "NGN", "ZAR", "INR"}


def resolve_screening_leg(
    from_asset: str,
    to_asset: str,
    from_amount: float,
    to_amount: float,
    usd_base_rates: Optional[dict] = None,
) -> tuple[str, float]:
    """
    Picks which leg of a swap/RFQ to report to ZIGRAM as
    Transaction_Currency/Transaction_Amount. ZIGRAM's Transaction_Currency is
    documented as a 3-char ISO code (e.g. INR, USD), so it can't hold a crypto
    ticker like AIRT or USDA.

    Prefers whichever leg is an actual fiat currency. If neither leg is fiat
    (a pure crypto<->crypto swap/RFQ), converts the from-leg into a KES
    equivalent using the same USD-base-rate convention already used by
    routes/ramp.py's _record_swap_profit, so ZIGRAM still gets a real,
    currency-coded amount rather than nothing.

    `usd_base_rates` should be the caller's already-merged
    {**DEFAULT_USD_BASE_RATES, **rate_book.get("usd_base_rates", {})} dict --
    this module intentionally doesn't import routes.treasury itself to avoid a
    services-depending-on-routes layering inversion.
    """
    from_asset = (from_asset or "").upper()
    to_asset = (to_asset or "").upper()

    if to_asset in _FIAT_CODES:
        return to_asset, float(to_amount or 0)
    if from_asset in _FIAT_CODES:
        return from_asset, float(from_amount or 0)

    if usd_base_rates:
        kes_per_usd = usd_base_rates.get("KES", 130.50)
        usd_per_unit = max(usd_base_rates.get(from_asset, 1.0), 1e-8)
        return "KES", round(float(from_amount or 0) * kes_per_usd / usd_per_unit, 2)

    # Last resort so callers never crash -- but confirm with ZIGRAM how they
    # want a pure crypto<->crypto leg represented when no rate book is available.
    return "KES", float(from_amount or 0)
