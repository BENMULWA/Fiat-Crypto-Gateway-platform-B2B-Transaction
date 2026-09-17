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

    def list_amm_pools(self) -> dict:
        return self._get("/api/v1/swap/pools")

    # --- Treasury / corridors (read-only) --------------------------------

    def list_corridor_nodes(self) -> dict:
        return self._get("/api/v1/admin/corridor/nodes")

    def rebalance_check(self) -> dict:
        return self._post("/api/v1/admin/corridor/rebalance/check", {})
