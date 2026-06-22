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
        
        # Webhook Callback URL (Make sure to update this in .env for production!)
        self.callback_url = os.getenv("LIPAD_CALLBACK_URL", "https://hemathermal-ha-dextrously.ngrok-free.dev/api/ramp/b2c/result")
        
    def get_access_token(self):
        """
        Authenticate with Lipad using Basic Auth on the /api/v1 endpoint.
        """
        api_url = f"{self.base_url}/api/v1"
        
        try:
            # Lipad requires a GET request with Basic Auth
            response = requests.get(
                api_url, 
                auth=(self.username, self.password)
            )
            response.raise_for_status()
            data = response.json()
            
            # Note: We fallback to checking both 'token' and 'access_token' just in case
            token = data.get('token') or data.get('access_token')
            if not token:
                print("\n❌ LIPAD AUTH ERROR: Logged in, but no token found in response:", data)
            return token
            
        except requests.exceptions.RequestException as e:
            error_msg = e.response.text if e.response is not None else str(e)
            print(f"\n❌ LIPAD AUTH ERROR DETAILED: {error_msg}\n")
            return None

    def execute_b2c_payout(self, phone_number: str, amount: int, transaction_id: str):
        """
        WITHDRAWAL: Send KES to the user's phone.
        Used when the user swaps USDA -> KES (Off-Ramp).
        """
        token = self.get_access_token()
        if not token:
            return {"status": "error", "message": "Authentication failed"}

        payout_url = f"{self.base_url}/api/v1/mobile/transfer"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        # Exact Payload format requested by the Docs for Payouts
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
                return {
                    "status": "success", 
                    "provider_id": data.get("transactionId"),
                    "secure_id": data.get("secureId"),
                    "message": "Payout initiated successfully"
                }
            else:
                return {"status": "error", "message": data.get("message", "API Error")}
                
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def execute_c2b_collection(self, phone_number: str, amount: int, transaction_id: str):
        """
        COLLECTION: Request KES from the user's phone via STK Push.
        Used when the user swaps KES -> USDA (On-Ramp).
        """
        token = self.get_access_token()
        if not token:
            return {"status": "error", "message": "Authentication failed"}

        collection_url = f"{self.base_url}/api/v1/mobile/initiate"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        # Exact Payload format requested by the Docs for Collections
        payload = {
            "impalaMerchantId": self.username,
            "displayName": "Mamlaka Swap", # Display Name shown on the user's M-Pesa Prompt
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
                return {
                    "status": "success", 
                    "provider_id": data.get("transactionId"),
                    "secure_id": data.get("secureId"),
                    "message": "Collection initiated successfully"
                }
            else:
                return {"status": "error", "message": data.get("message", "API Error")}
                
        except Exception as e:
            return {"status": "error", "message": str(e)}