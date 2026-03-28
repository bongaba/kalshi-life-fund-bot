import sys
import websockets
import asyncio
from dotenv import load_dotenv
load_dotenv()
import base64
import json
import time
import websockets
import os
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding

KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH")
MARKET_TICKER = "KXBTCD-26MAR2713-T65899.99"  # Replace with a real/active market ticker
WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"


def sign_pss_text(private_key, text: str) -> str:
    message = text.encode('utf-8')
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode('utf-8')

def create_headers(private_key, method: str, path: str) -> dict:
    timestamp = str(int(time.time() * 1000))
    msg_string = timestamp + method + path.split('?')[0]
    signature = sign_pss_text(private_key, msg_string)
    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }

async def kalshi_websocket():
    print(f"Python version: {sys.version}")
    print(f"websockets version: {websockets.__version__}")
    # Minimal test for extra_headers support
    test_headers = [("X-Test-Header", "test-value")]
    print("Connecting to echo.websocket.org for minimal test...")
    async with websockets.connect("wss://echo.websocket.org", additional_headers=test_headers) as websocket:
        print("✅ Connected to echo.websocket.org!")
        await websocket.send("hello")
        print("Echoed:", await websocket.recv())

    # Now test Kalshi WebSocket
    print("\nTesting Kalshi WebSocket...")
    try:
        with open(PRIVATE_KEY_PATH, "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)
        headers = create_headers(private_key, "GET", "/trade-api/ws/v2")
        print(f"Connecting to {WS_URL}...")
        async with websockets.connect(WS_URL, additional_headers=headers) as ws:
            print("✅ Connected to Kalshi WebSocket!")
            # Subscribe to the market
            # Try multiple subscribe formats to find the right one
            formats = [
                {"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_ticker": MARKET_TICKER}},
                {"id": 2, "cmd": "subscribe", "params": {"channel": "orderbook_delta", "market_ticker": MARKET_TICKER}},
                {"id": 3, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_tickers": [MARKET_TICKER]}},
                {"id": 4, "cmd": "subscribe", "params": {"channel": "orderbook_delta", "market_tickers": [MARKET_TICKER]}},
                {"id": 5, "cmd": "subscribe", "params": {"channels": ["orderbook"], "market_tickers": [MARKET_TICKER]}},
            ]
            for fmt in formats:
                await ws.send(json.dumps(fmt))
                print(f"Sent format id={fmt['id']}: {json.dumps(fmt)}")
            # Receive responses
            for i in range(10):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    print(f"Received: {msg}")
                except asyncio.TimeoutError:
                    print(f"No message received within 5s (attempt {i+1})")
                    if i == 4:
                        print("❌ No data received from Kalshi WebSocket")
                        break
    except Exception as e:
        print(f"❌ Failed to connect to Kalshi WebSocket: {e}")

if __name__ == "__main__":
    asyncio.run(kalshi_websocket())
