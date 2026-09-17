import os
import requests
from dotenv import load_dotenv

load_dotenv()

# CHANGES MADE:
# 1. Restored the get_access_token() method which was completely missing its logic body.
# 2. Added `.rstrip('/')` to the base_url to ensure the URL never constructs as '...com//api/v1', which causes 404 errors.

class DarajaService:
    def __init__(self):
        # Reads the SAME credentials as the live retail Mobile Money STK
        # flow (routes/ramp.py's on-ramp/off-ramp handlers) --
        # AIRTEL_API_USERNAME/AIRTEL_API_PASSWORD/AIRTEL_API_BASE_URL --
        # not the separate LIPAD_* vars this used to read. LIPAD/Daraja
        # credentials were never actually provisioned; the real Mam-laka
        # gateway account in production is configured under the AIRTEL_*
        # names, so this class silently could never succeed before, and
        # the corridor engine's merchant-balance check (the one caller of
        # this class, see Brain_Engine/state_engine.py) always got nothing.
        #
        # Also deliberately does NOT raise when a credential is missing.
        # This class is instantiated as a module-level singleton at import
        # time (Brain_Engine/state_engine.py's `daraja_service = DarajaService()`),
        # so raising here used to crash the entire backend at startup --
        # every unrelated route (retail wallets, KYC, admin dashboards) --
        # over one missing Mobile Money credential. Instead, a missing
        # credential is caught in get_access_token() below, at the point an
        # actual Mam-laka call is attempted, the same way every other
        # failure in this class already surfaces (a clean {"status": "error"}
        # instead of a crash).
        self.username = os.getenv("AIRTEL_API_USERNAME", "")
        self.password = os.getenv("AIRTEL_API_PASSWORD", "")

        # 🟢 Clean trailing slashes to prevent 404 URL errors
        raw_url = os.getenv("AIRTEL_API_BASE_URL", "https://sandbox.payments.mamlakapsp.com/api/v1")
        self.base_url = raw_url.rstrip('/')

        self.airtel_wallet = "073174090"

    def get_access_token(self):
        """
        🟢 FIXED: Authenticates using the correct GET /api/v1 endpoint with Basic Auth
        as outlined in your Postman Testing Guide!
        """
        if not self.username or not self.password:
            print("❌ Mam-laka Auth Error: AIRTEL_API_USERNAME / AIRTEL_API_PASSWORD is not configured")
            return None

        auth_url = f"{self.base_url}/api/v1"

        try:
            # Basic Auth is passed natively in the requests library
            response = requests.get(auth_url, auth=(self.username, self.password), timeout=15)
            
            if response.status_code in [200, 201]:
                return response.json().get("token")
            else:
                print(f"❌ Mam-laka Auth Error: {response.status_code} - {response.text}")
                return None
        except Exception as e:
            print(f"❌ Failed to connect to Mam-laka: {e}")
            return None

    def get_provider_from_phone(self, phone: str) -> str:
        """Auto-detects the telecom provider to prevent Mam-laka API failures."""
        if phone.startswith("071") or phone.startswith("072") or phone.startswith("079") or phone.startswith("070") or phone.startswith("011") or phone.startswith("25471") or phone.startswith("25472"):
            return "SAFARICOM"
        elif phone.startswith("073") or phone.startswith("078") or phone.startswith("010"):
            return "AIRTEL"
        elif phone.startswith("077"):
            return "TELKOM"
        return "SAFARICOM"

    def disburse_airtime(self, phone_number: str, amount: int, transaction_id: str, provider: str = None):
        """B2B PROCUREMENT: Safely deducts from your ARTM float and buys physical airtime."""
        token = self.get_access_token()
        if not token:
            return {"status": "error", "message": "Authentication failed"}

        actual_provider = provider if provider else self.get_provider_from_phone(phone_number)

        airtime_url = f"{self.base_url}/api/v1/mobile/airtime"
        headers = {
            "Authorization": f"Bearer {token}", 
            "Content-Type": "application/json"
        }

        payload = {
            "impalaMerchantId": self.username,
            "phone": phone_number,
            "amount": int(amount),
            "currency": "KES",
            "mobileMoneySP": actual_provider.capitalize(), 
            "externalId": transaction_id
        }

        try:
            response = requests.post(airtime_url, json=payload, headers=headers, timeout=15)
            if response.status_code in [200, 201]:
                data = response.json()
                return {"status": "success", "provider_id": data.get("transactionId", transaction_id)}
            else:
                return {"status": "error", "message": f"API Rejected: {response.text}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def collect_mobile_money(self, phone_number: str, amount: int, transaction_id: str, provider: str = None,
                              callback_url: str = None):
        """MOBILE MONEY COLLECTION (STK Push): pulls KES from a customer's
        M-Pesa or Airtel Money wallet into the merchant account.

        Same Mam-laka endpoint the retail (Jasiri) ramp flow uses for
        on-ramp deposits, generalized to take `provider` explicitly instead
        of hardcoding "M-Pesa" — that hardcoding is what silently routes
        Airtel Money requests to M-Pesa in the retail app today. Always
        pass provider explicitly for a liquidation node; only fall back to
        phone-based detection when the caller doesn't know it up front."""
        token = self.get_access_token()
        if not token:
            return {"status": "error", "message": "Authentication failed"}

        actual_provider = provider if provider else self.get_provider_from_phone(phone_number)
        initiate_url = f"{self.base_url}/api/v1/mobile/initiate"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "impalaMerchantId": self.username,
            "displayName": "Mamlaka IMM Collection",
            "currency": "KES",
            "amount": int(amount),
            "payerPhone": phone_number,
            "mobileMoneySP": actual_provider.capitalize(),
            "externalId": transaction_id,
            **({"callbackUrl": callback_url} if callback_url else {}),
        }

        try:
            response = requests.post(initiate_url, json=payload, headers=headers, timeout=15)
            if response.status_code in [200, 201]:
                data = response.json()
                return {"status": "success", "provider_id": data.get("transactionId", transaction_id)}
            return {"status": "error", "message": f"API Rejected: {response.text}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def payout_mobile_money(self, phone_number: str, amount: int, transaction_id: str, provider: str = None,
                             callback_url: str = None):
        """MOBILE MONEY PAYOUT (B2C): pushes KES from the merchant account
        out to an M-Pesa or Airtel Money wallet — this is the real,
        external settlement a LIQUIDATE leg needs to make its recognized
        float actually exist outside the ledger. Same generalization
        rationale as collect_mobile_money() above: provider is explicit,
        never assumed from the phone number for a known liquidation node."""
        token = self.get_access_token()
        if not token:
            return {"status": "error", "message": "Authentication failed"}

        actual_provider = provider if provider else self.get_provider_from_phone(phone_number)
        payout_url = f"{self.base_url}/api/v1/mobile/transfer"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "impalaMerchantId": self.username,
            "currency": "KES",
            "amount": int(amount),
            "recipientPhone": phone_number,
            "mobileMoneySP": actual_provider.capitalize(),
            "externalId": transaction_id,
            **({"callbackUrl": callback_url} if callback_url else {}),
        }

        try:
            response = requests.post(payout_url, json=payload, headers=headers, timeout=15)
            if response.status_code in [200, 201]:
                data = response.json()
                return {"status": "success", "provider_id": data.get("transactionId", transaction_id)}
            return {"status": "error", "message": f"API Rejected: {response.text}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_merchant_balance(self):
        """Fetches the live Web2 balances from Mam-laka's core ledger.

        The endpoint this used to call (/api/v1/merchant/balance) 404s
        against the real sandbox — confirmed live against
        sandbox.payments.mamlakapsp.com. The working one is
        /api/v1/wallet/balances, and the response key is lowercase
        "balances" (not "Balances"), containing kesBalance, artmBalance,
        airtelBalance, etc."""
        token = self.get_access_token()
        if not token:
            return {"status": "error", "message": "Authentication failed"}

        balance_url = f"{self.base_url}/api/v1/wallet/balances"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        try:
            response = requests.get(balance_url, headers=headers, timeout=15)
            if response.status_code not in (200, 201):
                return {"status": "error", "message": f"API Rejected: {response.text}"}

            data = response.json()
            if "balances" in data:
                return {"status": "success", "data": data["balances"]}
            return {"status": "success", "data": data}

        except Exception as e:
            return {"status": "error", "message": str(e)}
        

    def auto_sweep_kes_to_artm(self, amount: int):
        """Commands Mam-laka to convert collected KES into Airtime (ARTM) inventory."""
        token = self.get_access_token()
        if not token:
            return {"status": "error", "message": "Authentication failed"}

        sweep_url = f"{self.base_url}/api/v1/merchant/wallet-transfer"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        payload = {
            "impalaMerchantId": self.username,
            "fromWallet": "PAYINS",
            "toWallet": "ARTM",
            "amount": amount
        }

        try:
            response = requests.post(sweep_url, json=payload, headers=headers, timeout=15)
            if response.status_code in [200, 201]:
                return {"status": "success", "data": response.json()}
            else:
                return {"status": "error", "message": response.text}
        except Exception as e:
            return {"status": "error", "message": str(e)}