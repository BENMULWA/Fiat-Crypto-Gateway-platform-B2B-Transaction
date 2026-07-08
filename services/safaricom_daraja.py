import os
import requests
from dotenv import load_dotenv

# Force Python to read the .env file
load_dotenv()

class DarajaService:
    def __init__(self):
        # Grabbing the Lipad credentials from your .env file
        self.username = os.getenv("LIPAD_API_USERNAME")
        self.password = os.getenv("LIPAD_API_PASSWORD")
        self.base_url = os.getenv("LIPAD_BASE_URL", "https://payments.mam-laka.com")
        
        # 🟢 The dedicated wallet where your Airtel funds are sitting!
        self.airtel_wallet = "0782205361"
        
        # Webhook Callback URL
        self.callback_url = os.getenv("LIPAD_CALLBACK_URL", "https://hemathermal-ha-dextrously.ngrok-free.dev/api/ramp/b2c/result")
        
    def get_access_token(self):
        """Authenticate with Lipad using Basic Auth on the /api/v1 endpoint."""
        api_url = f"{self.base_url}/api/v1"
        try:
            response = requests.get(api_url, auth=(self.username, self.password))
            response.raise_for_status()
            data = response.json()
            token = data.get('token') or data.get('access_token')
            return token
        except requests.exceptions.RequestException as e:
            error_msg = e.response.text if e.response is not None else str(e)
            print(f"\n❌ LIPAD AUTH ERROR DETAILED: {error_msg}\n")
            return None

    def execute_b2c_payout(self, phone_number: str, amount: int, transaction_id: str):
        """WITHDRAWAL: Send KES to the user's phone (Off-Ramp)."""
        token = self.get_access_token()
        if not token:
            return {"status": "error", "message": "Authentication failed"}

        payout_url = f"{self.base_url}/api/v1/mobile/transfer"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        payload = {
            "impalaMerchantId": self.username,
            "currency": "KES",
            "amount": amount,
            "recipientPhone": phone_number,
            "mobileMoneySP": "M-Pesa",
            "externalId": transaction_id,
            "callbackUrl": self.callback_url
        }

        try:
            response = requests.post(payout_url, json=payload, headers=headers)
            data = response.json()
            if response.status_code in [200, 201] and data.get("message") == "Payment initiation successful":
                return {"status": "success", "provider_id": data.get("transactionId")}
            else:
                return {"status": "error", "message": data.get("message", "API Error")}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def execute_c2b_collection(self, phone_number: str, amount: int, transaction_id: str):
        """COLLECTION: Request KES from the user's phone via STK Push (On-Ramp)."""
        token = self.get_access_token()
        if not token:
            return {"status": "error", "message": "Authentication failed"}

        collection_url = f"{self.base_url}/api/v1/mobile/initiate"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        payload = {
            "impalaMerchantId": self.username,
            "displayName": "Mamlaka Swap", 
            "currency": "KES",
            "amount": amount,
            "payerPhone": phone_number,
            "mobileMoneySP": "M-Pesa",
            "externalId": transaction_id,
            "callbackUrl": self.callback_url
        }

        try:
            response = requests.post(collection_url, json=payload, headers=headers)
            data = response.json()
            if response.status_code in [200, 201] and data.get("message") == "Payment initiation successful":
                return {"status": "success", "provider_id": data.get("transactionId")}
            else:
                return {"status": "error", "message": data.get("message", "API Error")}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def disburse_airtime(self, phone_number: str, amount: int, transaction_id: str, provider: str = "AIRTEL"):
        """
        DISBURSEMENT: Send physical Airtime to a user's phone.
        🟢 Specifically uses the Airtel Wallet specified in __init__.
        """
        token = self.get_access_token()
        if not token:
            return {"status": "error", "message": "Authentication failed"}

        airtime_url = f"{self.base_url}/api/v1/mobile/airtime"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # 🟢 INJECTING YOUR SPECIFIC AIRTEL WALLET NUMBER
        payload = {
            "impalaMerchantId": self.username,
            "walletNumber": self.airtel_wallet,  
            "senderPhone": self.airtel_wallet,   
            "amount": amount,
            "phone": phone_number,  # 🔄 CHANGED BACK TO "phone" EXACTLY AS MAM-LAKA DEMANDED
            "mobileMoneySP": provider,
            "externalId": transaction_id
        }

        try:
            response = requests.post(airtime_url, json=payload, headers=headers)
            data = response.json()
            
            # 🚨 TRUTH TRACER 
            print(f"\n📡 MAM-LAKA RAW AIRTIME RESPONSE: {data}\n")
            
            if data.get("error") == "INSUFFICIENT_ARTM_BALANCE":
                return {"status": "error", "message": data.get("message", "Insufficient ARTM balance")}
                
            if response.status_code in [200, 201] and data.get("status") == "success":
                return {"status": "success", "provider_id": data.get("transactionId")}
            else:
                return {"status": "error", "message": data.get("message", "API Error")}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_merchant_balance(self):
        """
        Fetches the master merchant balances for dashboard UI.
        Silently returns 0 if Sandbox returns a 404 to avoid console spam.
        """
        token = self.get_access_token()
        if not token:
            return {"status": "error", "message": "Authentication failed"}

        balance_url = f"{self.base_url}/api/v1/merchant/balance"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        try:
            response = requests.get(balance_url, headers=headers)
            # Catch the 404 immediately before it throws a stack trace
            if response.status_code == 404:
                return {"status": "success", "data": {"artmBalance": 0.0}}
                
            response.raise_for_status()
            return {"status": "success", "data": response.json()}
        except Exception:
            # Absolute silent fallback to 0.0 for sandbox UI rendering
            return {"status": "success", "data": {"artmBalance": 0.0}}

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
            response = requests.post(sweep_url, json=payload, headers=headers)
            if response.status_code in [200, 201]:
                print(f"🔄 SWEEP SUCCESS: Converted {amount} KES to ARTM.")
                return {"status": "success", "data": response.json()}
            else:
                print(f"⚠️ SWEEP FAILED: {response.text}")
                return {"status": "error", "message": response.text}
        except Exception as e:
            print(f"❌ Error during auto-sweep: {e}")
            return {"status": "error", "message": str(e)}