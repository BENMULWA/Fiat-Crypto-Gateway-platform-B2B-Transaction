import os
import httpx
from typing import Dict, Any

class AirtelGatewayClient:
    def __init__(self):
        # Read from environment variables, fallback to defaults for safety
        self.base_url = os.environ.get("AIRTEL_GATEWAY_URL", "https://airtime.mamlakapsp.com")
        self.api_key = os.environ.get("AIRTEL_GATEWAY_API_KEY", "")
        
        self.headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json"
        }

    def _sanitize_phone(self, phone: str) -> str:
        """Ensures the phone number strictly matches the 254XXXXXXXXX format."""
        phone = phone.strip()
        if phone.startswith("+"):
            return phone[1:]
        elif phone.startswith("0"):
            return "254" + phone[1:]
        elif len(phone) == 9 and (phone.startswith("7") or phone.startswith("1")):
            return "254" + phone
        return phone

    async def initiate_stk_push(self, phone: str, amount: int, reference: str) -> Dict[str, Any]:
        """Triggers the STK Push via the Go Microservice asynchronously."""
        url = f"{self.base_url}/api/v1/stk/push"
        payload = {
            "phone_number": self._sanitize_phone(phone),
            "amount": amount,
            "reference": reference
        }

        # Use async httpx client so FastAPI can handle other requests concurrently
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url, 
                json=payload, 
                headers=self.headers, 
                timeout=15.0 # Fails gracefully if Nginx/Go hangs
            )
            response.raise_for_status() # Raises an exception for 4xx/5xx errors
            return response.json()

# Instantiate a singleton to be used across your FastAPI routes
airtel_gateway = AirtelGatewayClient()