import time
import json
import base64
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from config import *

# Load private key
private_key = None
try:
    with open(KALSHI_PRIVATE_KEY_PATH, "rb") as key_file:
        private_key = serialization.load_pem_private_key(
            key_file.read(),
            password=None,
            backend=default_backend()
        )
    print("Private key loaded successfully")
except Exception as e:
    print(f"Failed to load private key: {e}")
    raise

host = "https://demo-api.kalshi.co" if MODE == "demo" else "https://api.elections.kalshi.com"

def signed_request(method, endpoint, params=None, data=None):
    timestamp = str(int(time.time()))
    message = timestamp.encode('utf-8')
    signature = base64.b64encode(
        private_key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    ).decode('utf-8')
    
    headers = {
        'KALSHI-ACCESS-KEY': KALSHI_API_KEY_ID,
        'KALSHI-ACCESS-SIGNATURE': signature,
        'KALSHI-ACCESS-TIMESTAMP': timestamp,
        'Content-Type': 'application/json'
    }
    
    url = f"{host}/trade-api/v2{endpoint}"
    response = requests.request(method, url, headers=headers, params=params, json=data)
    response.raise_for_status()
    return response.json()

def fetch_historical_markets(days=210, max_pages=20):
    current_seconds = int(time.time())
    min_close_seconds = current_seconds - (days * 24 * 3600)

    params = {
        "limit": 1000,
        "status": "closed",
        "min_close_ts": min_close_seconds
    }

    all_markets = []
    cursor = None
    page = 1

    while page <= max_pages:
        if cursor:
            params["cursor"] = cursor
        try:
            data = signed_request("GET", "/markets", params=params)
        except requests.exceptions.HTTPError as e:
            if e.response and e.response.status_code == 429:
                print("Rate limit hit — sleeping 60s")
                time.sleep(60)
                break
            raise
        markets_page = data.get('markets', [])
        all_markets.extend(markets_page)
        cursor = data.get('cursor')
        print(f"Page {page}: {len(markets_page)} historical markets")
        page += 1
        if not cursor:
            break

    return all_markets

if __name__ == "__main__":
    print("Fetching historical markets...")
    historical_markets = fetch_historical_markets(days=30, max_pages=10)
    print(f"Fetched {len(historical_markets)} historical markets")

    # Save to JSON
    with open('historical_markets.json', 'w') as f:
        json.dump(historical_markets, f, indent=2, default=str)
    print("Saved to historical_markets.json")