import requests
import time
import base64
from urllib.parse import urlparse
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding
from config import KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, MODE

# Configuration
BASE_URL = "https://demo-api.kalshi.co/trade-api/v2" if MODE == "demo" else "https://api.elections.kalshi.com/trade-api/v2"

def load_private_key(key_path):
    with open(key_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def create_signature(private_key, timestamp, method, path):
    """Create the request signature."""
    # Strip query parameters before signing
    path_without_query = path.split('?')[0]
    message = f"{timestamp}{method}{path_without_query}".encode('utf-8')
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode('utf-8')

def post(private_key, api_key_id, path, body, base_url=BASE_URL):
    """Make an authenticated POST request to the Kalshi API."""
    timestamp = str(int(time.time() * 1000))
    sign_path = urlparse(base_url + path).path
    signature = create_signature(private_key, timestamp, "POST", sign_path)

    headers = {
        'KALSHI-ACCESS-KEY': api_key_id,
        'KALSHI-ACCESS-SIGNATURE': signature,
        'KALSHI-ACCESS-TIMESTAMP': timestamp,
        'Content-Type': 'application/json'
    }

    print(f"POST Timestamp: {timestamp}")
    print(f"POST Sign Path: {sign_path}")
    print(f"POST Message: {timestamp}POST{sign_path}")
    print(f"POST Signature: {signature[:50]}...")  # Truncate for brevity
    print(f"POST URL: {base_url + path}")
    print(f"POST Body: {body}")

    response = requests.post(base_url + path, headers=headers, json=body)
    print(f"POST Response Status: {response.status_code}")
    if response.status_code in [200, 201]:
        print("Order placed successfully!")
        print(f"Response: {response.json()}")
    else:
        print(f"Error: {response.text}")
def get(private_key, api_key_id, path, base_url=BASE_URL):
    """Make an authenticated GET request to the Kalshi API."""
    timestamp = str(int(time.time() * 1000))
    sign_path = urlparse(base_url + path).path
    signature = create_signature(private_key, timestamp, "GET", sign_path)

    headers = {
        'KALSHI-ACCESS-KEY': api_key_id,
        'KALSHI-ACCESS-SIGNATURE': signature,
        'KALSHI-ACCESS-TIMESTAMP': timestamp,
    }

    print(f"GET Timestamp: {timestamp}")
    print(f"GET Sign Path: {sign_path}")
    print(f"GET Message: {timestamp}GET{sign_path}")
    print(f"GET Signature: {signature[:50]}...")  # Truncate for brevity
    print(f"GET URL: {base_url + path}")

    response = requests.get(base_url + path, headers=headers)
    print(f"GET Response Status: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        balance = data.get('balance', 0) / 100
        portfolio_value = data.get('portfolio_value', 0) / 100
        total_value = balance + portfolio_value
        
        print("✅ Balance retrieved successfully!")
        print(f"💰 Balance: ${balance:.2f}")
        print(f"📊 Portfolio Value: ${portfolio_value:.2f}")
        print(f"🏆 Total Account Value: ${total_value:.2f}")
        print(f"🕒 Last Updated: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data.get('updated_ts', 0)))}")
        print(f"📋 Raw Response: {data}")
    else:
        print(f"❌ Error: {response.text}")
    return response
    """Fetch a few open markets."""
    params = "?limit=10&status=open"
    return get(private_key, api_key_id, "/markets" + params, base_url)

# Load private key
private_key = load_private_key(KALSHI_PRIVATE_KEY_PATH)

# Get balance
print(f"Testing with MODE: {MODE}")
print(f"BASE_URL: {BASE_URL}")
print(f"API_KEY_ID: {KALSHI_API_KEY_ID}")
response = get(private_key, KALSHI_API_KEY_ID, "/portfolio/balance")