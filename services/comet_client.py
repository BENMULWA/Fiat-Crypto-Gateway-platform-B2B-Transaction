import os
import requests
from dotenv import load_dotenv

load_dotenv()


class CometClient:
    """Thin wrapper over Comet Engine's IMM / AMM / vault API
    (docs.mamlakapsp.com). Every call returns {"status": "success", ...}
    or {"status": "error", "message": ...} — same convention as
    DarajaService — so callers never need to catch requests exceptions
    themselves.

    Confirmed against production (cometpayments.mamlakapsp.com) on
    2026-09-11: X-API-Key/X-API-Secret alone is accepted on every admin
    endpoint tested (rates, vault, exposure, corridor nodes, risk/pnl) —
    no X-Service-Token was required there, despite the docs describing
    those as service-token-gated. Pass service_token explicitly only for
    calls the docs say are service-token-only (tenant admin, wallet-config,
    Stellar pool admin) and fall back to API key/secret first.
    """

    def __init__(self):
        raw_url = os.getenv("COMET_BASE_URL", "https://cometpayments.mamlakapsp.com")
        self.base_url = raw_url.rstrip("/")
        self.api_key = os.getenv("COMET_API_KEY", "")
        self.api_secret = os.getenv("COMET_API_SECRET", "")
        self.service_token = os.getenv("COMET_SERVICE_TOKEN", "")
        self.tenant_slug = os.getenv("COMET_TENANT_SLUG", "comet")

    def _headers(self, use_service_token: bool = False) -> dict:
        if use_service_token and self.service_token:
            return {"X-Service-Token": self.service_token, "Content-Type": "application/json"}
        return {
            "X-API-Key": self.api_key,
            "X-API-Secret": self.api_secret,
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict | None = None, use_service_token: bool = False) -> dict:
        try:
            response = requests.get(
                f"{self.base_url}{path}",
                params=params,
                headers=self._headers(use_service_token),
                timeout=15,
            )
            if response.status_code not in (200, 201):
                return {"status": "error", "message": response.text, "statusCode": response.status_code}
            return {"status": "success", "data": response.json()}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _post(self, path: str, payload: dict, use_service_token: bool = False) -> dict:
        try:
            response = requests.post(
                f"{self.base_url}{path}",
                json=payload,
                headers=self._headers(use_service_token),
                timeout=15,
            )
            if response.status_code not in (200, 201, 202):
                return {"status": "error", "message": response.text, "statusCode": response.status_code}
            return {"status": "success", "data": response.json()}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # --- IMM: pricing & execution -------------------------------------

    def get_imm_quote(self, base: str, quote: str, amount_in: float) -> dict:
        """GET /api/v1/imm/quote — no side effects. Comet itself refuses
        quotes on a rate older than 60 minutes (400 "no rate set"/stale),
        so there's no separate staleness check to duplicate here."""
        return self._get("/api/v1/imm/quote", {"base": base, "quote": quote, "amountIn": amount_in})

    def set_imm_rate(self, base: str, quote: str, rate: float) -> dict:
        """POST /api/v1/admin/imm/rates. `rate` MUST be numeric — the docs
        show it as a quoted string, but the live server rejects that with
        'cannot unmarshal string into Go struct field .rate of type float64'."""
        return self._post("/api/v1/admin/imm/rates", {"base": base, "quote": quote, "rate": float(rate)})

    def list_imm_rates(self) -> dict:
        return self._get("/api/v1/admin/imm/rates")

    def execute_imm_swap(self, external_user_id: str, chain: str, base: str, quote: str,
                          amount_in: float, external_id: str) -> dict:
        """POST /api/v1/imm/swap. Response includes vaultBacked: bool —
        surface that to the caller/UI, it's the honest signal for whether
        this payout came from real reserves or a synthetic mint."""
        return self._post("/api/v1/imm/swap", {
            "externalUserId": external_user_id,
            "chain": chain,
            "base": base,
            "quote": quote,
            "amountIn": amount_in,
            "externalId": external_id,
        })

    # --- IMM: vault & risk (read-only) ---------------------------------

    def get_vault(self, chain: str, asset: str) -> dict:
        return self._get("/api/v1/admin/imm/vault", {"chain": chain, "asset": asset})

    def get_exposure(self, chain: str, asset: str) -> dict:
        return self._get("/api/v1/admin/imm/exposure", {"chain": chain, "asset": asset})

    def get_risk_pnl(self, chain: str, asset: str, since_days: int = 7) -> dict:
        return self._get("/api/v1/admin/risk/pnl", {"chain": chain, "asset": asset, "sinceDays": since_days})

    # --- Celo AMM (Uniswap V2) ------------------------------------------

    def get_amm_quote(self, from_symbol: str, to_symbol: str, amount_in: str) -> dict:
        return self._get("/api/v1/swap/quote", {"from": from_symbol, "to": to_symbol, "amountIn": amount_in})

    def execute_amm_swap(self, external_user_id: str, from_symbol: str, to_symbol: str, amount_in: str,
                          tenant_slug: str | None = None) -> dict:
        """POST /api/v1/swap/tokens — the real Celo AMM execute endpoint
        (docs.mamlakapsp.com/api/amm.html), NOT /api/v1/imm/swap. Used for
        the corridor's IMC/USDT/USDC legs: Comet lists USDT/USDC, USDT/IMC,
        USDC/IMC as real supported pools. amount_in is a base-unit string
        (see tokenize_airtime's docstring on units) — the caller converts.

        externalUserId, NOT the numeric userId the tokenization.html/
        amm.html doc examples show — confirmed 2026-09-25 against the real
        API: GET /api/v1/assets/{symbol}/balance flatly rejects a numeric
        userId ("externalUserId is required") and only resolves once a
        real wallet exists for that externalUserId string (POST
        /api/v1/wallets/create). The doc examples' numeric literals appear
        to be wrong/inconsistent with live behavior — trust this, not them."""
        payload = {"externalUserId": external_user_id, "from": from_symbol, "to": to_symbol, "amountIn": amount_in}
        if tenant_slug:
            payload["tenantSlug"] = tenant_slug
        return self._post("/api/v1/swap/tokens", payload)

    def list_amm_pools(self) -> dict:
        return self._get("/api/v1/swap/pools")

    # --- Tokenization (airtime-backed IMC) ------------------------------
    # docs.mamlakapsp.com/api/tokenization.html. Distinct from the IMM
    # swap/rate endpoints above (those serve the admin-priced OTC spread
    # board — Spread Engine tab); this is the airtime-backed proof-of-
    # reserve mint the corridor's MINT step actually needs: the invariant
    # Comet enforces is "on-chain IMC supply = real airtime float
    # remaining." Comet does NOT verify reserve availability itself — the
    # docs are explicit: "The caller (typically app-core-backend) must
    # verify the ImpalaPay float balance before calling this endpoint."
    # That check is Brain_Engine.risk_engine.check_airtime_backing, called
    # before every call here, not something this client can skip past.

    IMC_DECIMALS = 6

    @staticmethod
    def to_base_units(amount: float, decimals: int = IMC_DECIMALS) -> str:
        return str(int(round(amount * (10 ** decimals))))

    @staticmethod
    def from_base_units(amount_base: str, decimals: int = IMC_DECIMALS) -> float:
        return float(amount_base) / (10 ** decimals)

    def tokenize_airtime(self, external_user_id: str, amount_base: str, external_id: str, chain: str = "celo") -> dict:
        """POST /api/v1/tokenize/airtime. amount_base is a base-unit STRING
        with 6 decimals (docs example: "10000000" == 10 IMC) — always
        convert with to_base_units(), never pass a raw float amount.

        externalUserId, not numeric userId — see execute_amm_swap's
        docstring for the confirmed real evidence this is based on. NOT
        yet directly confirmed against this specific endpoint (only the
        balance/wallets endpoints were tested live) — a small real
        tokenize_airtime call is worth confirming before relying on this
        for a production-size mint."""
        return self._post("/api/v1/tokenize/airtime", {
            "externalUserId": external_user_id, "amountBase": amount_base, "externalId": external_id, "chain": chain,
        })

    def burn_imc(self, external_user_id: str, amount_base: int, external_id: str) -> dict:
        """POST /api/v1/assets/imc/burn-by-holder — redemption path (e.g.
        an eventual real airtime delivery against held IMC), not used by
        the corridor's exit leg today (that uses execute_amm_swap instead,
        since the corridor exits to USDC, not to a physical redemption)."""
        return self._post("/api/v1/assets/imc/burn-by-holder", {
            "externalUserId": external_user_id, "amountBase": amount_base, "externalId": external_id,
        })

    def get_tokenize_status(self, tokenize_id: str) -> dict:
        return self._get(f"/api/v1/tokenize/status/{tokenize_id}")

    def get_or_create_wallet(self, external_user_id: str, chain: str = "celo") -> dict:
        """POST /api/v1/wallets/create — docs.mamlakapsp.com/api/wallets.html.
        Idempotent: returns the existing wallet if one already exists for
        this (tenant, externalUserId, chain family) rather than creating a
        duplicate. This is the real, Comet-CUSTODIED address that
        execute_imm_swap/execute_amm_swap operate against — NOT the same
        wallet tokenize_airtime mints into (that mints to whatever
        self-custodied treasury address the caller controls). Confirmed
        live 2026-09-28: a real mint's IMC sat in the treasury's own
        wallet, and a swap attempt failed with "wallet ... holds 0.000000
        IMC" against this Comet wallet — the two are genuinely different
        addresses, and nothing moves between them automatically."""
        return self._post("/api/v1/wallets/create", {"externalUserId": external_user_id})

    def send_asset(self, symbol: str, external_user_id: str, to: str, amount_base: str) -> dict:
        """POST /api/v1/assets/{symbol}/send — docs.mamlakapsp.com/api/
        assets.html. Withdraws from a user's Comet-managed custodial wallet
        to an arbitrary external address (EVM 0x... or Stellar G...). This
        is the step _execute_comet_exit needs after swapping to USDC: the
        AMM swap alone only settles inside Comet's own custody for this
        user, it does not move funds to CELO_EXIT_ADDRESS (a separate,
        self-custodied wallet) on its own.

        Uses `externalUserId` (string), NOT the numeric `userId` tokenize/
        AMM calls take — per docs.mamlakapsp.com/api/wallets.html, wallet
        identity is string-based. Assumed here that str(COMET_USER_ID) is
        the same identity Comet's wallet system already knows from the
        tokenize/AMM calls made under that numeric id — unconfirmed by any
        single doc page, but it's the only consistent reading across both
        conventions Comet's docs actually show."""
        return self._post(f"/api/v1/assets/{symbol.lower()}/send", {
            "externalUserId": external_user_id, "to": to, "amountBase": amount_base,
        })

    # --- Treasury / corridors (read-only) --------------------------------

    def list_corridor_nodes(self) -> dict:
        return self._get("/api/v1/admin/corridor/nodes")

    def rebalance_check(self) -> dict:
        return self._post("/api/v1/admin/corridor/rebalance/check", {})
