import os
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

# Force Python to read the .env file
load_dotenv()

class DarajaService:
    def __init__(self):
        # Grabbing the Lipad credentials from your .env file
        self.username = os.getenv("LIPAD_API_USERNAME")
        self.password = os.getenv("LIPAD_API_PASSWORD")
        
        # Use the actual URL from the documentation you provided
        self.base_url = os.getenv("LIPAD_BASE_URL", "https://payments.mam-laka.com")
        
    def get_access_token(self):
        """
        Authenticate with Lipad using Basic Auth.
        Matches: GET {{baseUrl}}/api/v1
        """
        api_url = f"{self.base_url}/api/v1"
        
        try:
            # HTTPBasicAuth automatically encodes the username:password to base64
            # exactly as requested in the docs: Authorization: Basic Y29...
            response = requests.get(
                api_url, 
                auth=HTTPBasicAuth(self.username, self.password)
            )
            response.raise_for_status()
            data = response.json()
            
            # The token is returned in the response
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
        Send KES to the user's phone via Lipad (Off-Ramp).
        Matches: POST /api/v1/mobile/transfer
        """
        token = self.get_access_token()
        if not token:
            return {"status": "error", "message": "Authentication failed"}

        api_url = f"{self.base_url}/api/v1/mobile/transfer"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        # Exact Payload format requested by the Mamlaka API v3.0.0 Docs
        payload = {
            "impalaMerchantId": self.username, # Uses 'meshex_sandbox'
            "currency": "KES",
            "amount": amount,
            "recipientPhone": phone_number,
            "mobileMoneySP": "M-Pesa",
            "externalId": transaction_id,
            # KEEP THIS AS YOUR NGROK URL FOR TESTING!
            "callbackUrl": "https://hemathermal-ha-dextrously.ngrok-free.dev/api/ramp/b2c/result"
        }

        try:
            response = requests.post(api_url, json=payload, headers=headers)
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