import time
import sqlite3
import requests
import base64
import json
from loguru import logger
from logging_setup import setup_log_file
from config import *
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

setup_log_file("monitor.log")

host = "https://demo-api.kalshi.co" if MODE == "demo" else "https://api.elections.kalshi.com"

# Load private key
private_key = None
try:
    with open(KALSHI_PRIVATE_KEY_PATH, "rb") as key_file:
        private_key = serialization.load_pem_private_key(
            key_file.read(),
            password=None,
            backend=default_backend()
        )
    logger.info("Private key loaded successfully")
except Exception as e:
    logger.error(f"Failed to load private key: {e}")
    raise

def create_signature(private_key, timestamp, method, path):
    """Create Kalshi request signature"""
    path_without_query = path.split('?')[0]
    message = f"{timestamp}{method}{path_without_query}".encode('utf-8')
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode('utf-8')

def signed_request(method, path, params=None, body=None):
    timestamp = str(int(time.time() * 1000))

    full_path = "/trade-api/v2" + path
    if params:
        full_path += "?" + "&".join(f"{k}={v}" for k, v in params.items())

    sign_path = ("/trade-api/v2" + path).split('?')[0]
    message = f"{timestamp}{method.upper()}{sign_path}"

    signature = create_signature(private_key, timestamp, method.upper(), sign_path)

    headers = {
        "Content-Type": "application/json" if body else None,
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }

    url = f"{host}{full_path}"

    if method.upper() == "GET":
        response = requests.get(url, headers=headers)
    elif method.upper() == "POST":
        response = requests.post(url, headers=headers, json=body)
    else:
        raise ValueError(f"Unsupported method: {method}")

    response.raise_for_status()
    return response.json()

def get_current_positions():
    """Get current open positions from Kalshi"""
    try:
        logger.info("Fetching positions from API...")
        data = signed_request("GET", "/portfolio/positions")
        positions = data.get('market_positions', [])
        logger.info(f"API returned {len(positions)} market positions")
        return positions
    except Exception as e:
        logger.error(f"Failed to get positions: {e}")
        logger.error(f"Response details: {getattr(e, 'response', None)}")
        return []

def get_market_price(ticker):
    """Get current market price for a ticker"""
    try:
        data = signed_request("GET", f"/markets/{ticker}")
        market = data.get('market', {})
        yes_bid = market.get('yes_bid', 0) / 100.0
        yes_ask = market.get('yes_ask', 0) / 100.0
        return (yes_bid + yes_ask) / 2 if yes_bid > 0 and yes_ask > 0 else 0.5
    except Exception as e:
        logger.error(f"Failed to get market price for {ticker}: {e}")
        return 0.5

def monitor_positions():
    """Monitor current positions and calculate potential P&L"""
    logger.info("🔍 Checking current positions...")

    positions = get_current_positions()
    if not positions:
        logger.info("No open positions found")
        return

    conn = sqlite3.connect('trades.db')
    cursor = conn.cursor()

    total_unrealized_pnl = 0
    profitable_positions = []

    for position in positions:
        ticker = position.get('ticker')
        if not ticker:
            continue

        # Get our trade info from database
        cursor.execute("""
            SELECT direction, size, price FROM trades
            WHERE market_ticker = ? AND status = 'OPEN'
        """, (ticker,))
        trade = cursor.fetchone()

        if not trade:
            continue

        direction, size, entry_price = trade
        current_price = get_market_price(ticker)

        # Calculate unrealized P&L
        if direction == 'YES':
            unrealized_pnl = size * (current_price - entry_price)
        else:  # NO
            unrealized_pnl = size * (entry_price - current_price)

        total_unrealized_pnl += unrealized_pnl

        logger.info(f"📊 {ticker} | {direction} | Size: ${size:.2f} | Entry: {entry_price:.3f} | Current: {current_price:.3f} | P&L: ${unrealized_pnl:.2f}")

        # Track profitable positions
        if unrealized_pnl > 0.5:  # More than $0.50 profit
            profitable_positions.append({
                'ticker': ticker,
                'direction': direction,
                'size': size,
                'pnl': unrealized_pnl
            })

    logger.info(f"💰 Total Unrealized P&L: ${total_unrealized_pnl:.2f}")

    # Note: In Kalshi, you can't sell positions - they auto-resolve
    # This is for monitoring only
    if profitable_positions:
        logger.info("✅ Profitable positions:")
        for pos in profitable_positions:
            logger.info(f"   {pos['ticker']} | {pos['direction']} | +${pos['pnl']:.2f}")

    conn.close()

if __name__ == "__main__":
    monitor_positions()