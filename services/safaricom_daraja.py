import os
import requests
from dotenv import load_dotenv

load_dotenv()

# CHANGES MADE:
# 1. Restored the get_access_token() method which was completely missing its logic body.
# 2. Added `.rstrip('/')` to the base_url to ensure the URL never constructs as '...com//api/v1', which causes 404 errors.

class DarajaService:
    def __init__(self):
        self.username = os.getenv("LIPAD_API_USERNAME", "meshex_sandbox")
        self.password = os.getenv("LIPAD_API_PASSWORD", "mesh94DjsuSans8w203@2046ex")
        
        # 🟢 Clean trailing slashes to prevent 404 URL errors
        raw_url = os.getenv("LIPAD_BASE_URL", "https://payments.mam-laka.com")
        self.base_url = raw_url.rstrip('/')
        
        self.airtel_wallet = "073174090"  
        
    def get_access_token(self):
        """
        🟢 FIXED: Authenticates using the correct GET /api/v1 endpoint with Basic Auth 
        as outlined in your Postman Testing Guide!
        """
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

    def get_merchant_balance(self):
        """Fetches the live Web2 balances from Mam-laka's core ledger."""
        token = self.get_access_token()
        if not token:
            return {"status": "error", "message": "Authentication failed"}

        # 🟢 FIXED: Updated to match the Postman Guide endpoint
        balance_url = f"{self.base_url}/api/v1/merchant/balance"
        
        headers = {
            "Authorization": f"Bearer {token}", 
            "Content-Type": "application/json"
        }

        try:
            response = requests.get(balance_url, headers=headers, timeout=15)
            data = response.json()
            
            if "Balances" in data:
                return {"status": "success", "data": data["Balances"]}
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