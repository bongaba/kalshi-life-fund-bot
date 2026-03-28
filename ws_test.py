import asyncio
import websockets
import os
import json
import base64
import time
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes

KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

def create_signature(private_key, timestamp, method, path):
    message = f"{timestamp}{method}{path}".encode('utf-8')
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode('utf-8')

async def ws_test():
    api_key_id = os.getenv("KALSHI_API_KEY_ID")
    private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key_id or not private_key_path:
        print("ERROR: KALSHI_API_KEY_ID and/or KALSHI_PRIVATE_KEY_PATH are missing from .env!")
        return
    # Load private key
    with open(private_key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    
    # Auth headers
    method = "GET"
    path = "/trade-api/ws/v2"
    timestamp = str(int(time.time() * 1000))
    signature = create_signature(private_key, timestamp, method, path)
    headers = [
        ("Content-Type", "application/json"),
        ("KALSHI-ACCESS-KEY", api_key_id),
        ("KALSHI-ACCESS-SIGNATURE", signature),
        ("KALSHI-ACCESS-TIMESTAMP", timestamp),
    ]
    async with websockets.connect(KALSHI_WS_URL, additional_headers=headers) as ws:
        print("WebSocket connected!")
        # Subscribe to a known active ticker (replace with a real one if needed)
        ticker = "PI_XBTUSD"
        sub_msg = {
            "type": "subscribe",
            "channels": [
                {"name": "orderbook_delta", "product_id": ticker}
            ]
        }
        await ws.send(json.dumps(sub_msg))
        print(f"Subscribed to {ticker}")
        # Print the first 10 messages received
        for i in range(10):
            msg = await ws.recv()
            print(f"[WS RAW] {msg}")

if __name__ == "__main__":
    asyncio.run(ws_test())
