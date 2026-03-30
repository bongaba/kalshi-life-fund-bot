import asyncio
import websockets
import json
import os
import base64
import time
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
import logging
from typing import Dict, Any, Callable, Set

# Configure logging
logger = logging.getLogger("kalshi_ws")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)

KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"


class KalshiWebSocketClient:
    def __init__(self, api_key_id: str, private_key_path: str, tickers: Set[str], on_orderbook: Callable[[str, str, dict], None]):
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self.tickers = set(tickers)
        self.on_orderbook = on_orderbook
        self.ws = None
        self._connected = False
        self._lock = asyncio.Lock()
        # Read private key file contents as secret
        try:
            with open(private_key_path, "r") as f:
                self.private_key_contents = f.read()
        except Exception as e:
            self.private_key_contents = ""
            logger.error(f"Failed to read private key file for WebSocket: {e}")

    async def connect(self):
        """Connect to Kalshi WS with automatic reconnection on failure."""
        self._loop = asyncio.get_event_loop()
        backoff = 1  # seconds
        max_backoff = 60
        while True:
            try:
                async with websockets.connect(
                    KALSHI_WS_URL,
                    additional_headers=self._auth_headers(),
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    self.ws = ws
                    self._connected = True
                    backoff = 1  # reset on successful connect
                    logger.info("WebSocket connected. Subscribing to tickers...")
                    await self._subscribe_all()
                    await self._listen()
            except Exception as e:
                self._connected = False
                self.ws = None
                logger.warning(f"WebSocket disconnected: {e}. Reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    @property
    def is_connected(self):
        return self._connected and self.ws is not None

    async def subscribe_ticker(self, ticker: str):
        async with self._lock:
            if ticker not in self.tickers:
                self.tickers.add(ticker)
                if self._connected and self.ws:
                    try:
                        sub_msg = {
                            "id": int(time.time() * 1000),
                            "cmd": "subscribe",
                            "params": {
                                "channels": ["orderbook_delta"],
                                "market_ticker": ticker
                            }
                        }
                        await self.ws.send(json.dumps(sub_msg))
                        logger.info(f"Dynamically subscribed to orderbook_delta for {ticker}")
                    except Exception as e:
                        logger.warning(f"Failed to subscribe {ticker}: {e}. Will retry on reconnect.")
                else:
                    logger.warning(f"WS not connected — {ticker} queued for subscription on reconnect.")

    def _auth_headers(self):
        # Kalshi WebSocket authentication: signed headers
        method = "GET"
        path = "/trade-api/ws/v2"
        timestamp = str(int(time.time() * 1000))
        msg_string = timestamp + method + path
        # Load private key
        private_key = serialization.load_pem_private_key(
            self.private_key_contents.encode(),
            password=None
        )
        signature = private_key.sign(
            msg_string.encode('utf-8'),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        signature_b64 = base64.b64encode(signature).decode('utf-8')
        headers = [
            ("Content-Type", "application/json"),
            ("KALSHI-ACCESS-KEY", self.api_key_id),
            ("KALSHI-ACCESS-SIGNATURE", signature_b64),
            ("KALSHI-ACCESS-TIMESTAMP", timestamp),
        ]
        return headers

    async def _subscribe_all(self):
        for ticker in self.tickers:
            sub_msg = {
                "id": int(time.time() * 1000),
                "cmd": "subscribe",
                "params": {
                    "channels": ["orderbook_delta"],
                    "market_ticker": ticker
                }
            }
            await self.ws.send(json.dumps(sub_msg))
            logger.info(f"Subscribed to orderbook_delta for {ticker}")

    async def _listen(self):
        async for msg in self.ws:
            logger.debug(f"[WS RAW] {msg}")
            try:
                data = json.loads(msg)
                msg_type = data.get("type", "")
                if msg_type in ("orderbook_snapshot", "orderbook_delta"):
                    inner = data.get("msg", {})
                    ticker = inner.get("market_ticker", "")
                    if ticker in self.tickers:
                        self.on_orderbook(ticker, msg_type, inner)
                elif msg_type == "error":
                    logger.warning(f"WS error: {data}")
            except Exception as e:
                logger.error(f"Error processing message: {e}")

# Example usage:
if __name__ == "__main__":
    # Load credentials from environment or .env
    API_KEY = os.getenv("KALSHI_API_KEY")
    API_SECRET = os.getenv("KALSHI_API_SECRET")
    TICKERS = {"PI_XYZ23", "PI_ABC45"}  # Replace with your tickers

    def handle_orderbook(ticker: str, msg_type: str, data: dict):
        # Only use real, executable quotes (never synthetic or implied)
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        # Example: print best bid/ask
        if bids and asks:
            logger.info(f"{ticker} | Best Bid: {bids[0]} | Best Ask: {asks[0]}")
        else:
            logger.info(f"{ticker} | No valid quotes.")

    client = KalshiWebSocketClient(API_KEY, API_SECRET, TICKERS, handle_orderbook)
    asyncio.run(client.connect())
