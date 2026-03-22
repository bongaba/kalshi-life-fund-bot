import time
import sqlite3
import schedule
import requests
import uuid
import base64
import json
from loguru import logger
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from config import *
from decision_engine import should_trade
import winsound

logger.add("bot.log", rotation="1 day")

# Startup debug
logger.info(f"[BOT START] MODE: {MODE}")
logger.info(f"[BOT START] RISK_PER_TRADE: {RISK_PER_TRADE}")
logger.info(f"[BOT START] XAI_API_KEY (first 5 chars): {XAI_API_KEY[:5] if XAI_API_KEY else 'None'}...")

host = "https://api.elections.kalshi.com/trade-api/v2"

# Load private key from file
def load_private_key_from_file(file_path):
    with open(file_path, "rb") as key_file:
        private_key = serialization.load_pem_private_key(
            key_file.read(),
            password=None,
            backend=default_backend()
        )
    return private_key

# Sign text with PSS
def sign_pss_text(private_key, text: str) -> str:
    message = text.encode('utf-8')
    try:
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode('utf-8')
    except Exception as e:
        logger.error(f"Signing failed: {e}")
        raise

try:
    private_key = load_private_key_from_file(KALSHI_PRIVATE_KEY_PATH)
    logger.info("Private key loaded successfully")
except Exception as e:
    logger.error(f"Failed to load private key: {e}")
    raise

# Database setup
conn = sqlite3.connect('trades.db')
conn.execute('''CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    market_ticker TEXT,
    direction TEXT,
    size REAL,
    price REAL,
    pnl REAL DEFAULT 0,
    reason TEXT,
    status TEXT DEFAULT 'PLACED'
)''')
conn.commit()

daily_loss = 0.0

def daily_pnl_reset():
    global daily_loss
    daily_loss = 0.0
    logger.info("Daily loss limit reset")

schedule.every().day.at("00:00").do(daily_pnl_reset)

def play_trade_notification():
    try:
        winsound.Beep(1200, 150)
        winsound.Beep(1400, 150)
        winsound.Beep(1600, 150)
        winsound.Beep(1000, 400)
        logger.info("Audio played")
    except Exception as e:
        logger.warning(f"Audio failed: {e}")

def update_trade_status(ticker, new_status, pnl=None):
    cursor = conn.cursor()
    if pnl is not None:
        cursor.execute('''
            UPDATE trades SET status = ?, pnl = ? WHERE market_ticker = ? AND status NOT IN ('WON', 'LOST')
        ''', (new_status, pnl, ticker))
    else:
        cursor.execute('''
            UPDATE trades SET status = ? WHERE market_ticker = ? AND status NOT IN ('WON', 'LOST')
        ''', (new_status, ticker))
    conn.commit()
    logger.info(f"Updated status for {ticker} to {new_status}")

def signed_request(method, path, params=None, body=None):
    timestamp = str(int(time.time() * 1000))

    full_path = path
    if params:
        full_path += "?" + "&".join(f"{k}={v}" for k, v in params.items())

    # Body as compact sorted JSON (for POST body, not for signing)
    if body and isinstance(body, dict):
        body_str = json.dumps(body, separators=(',', ':'), sort_keys=True)
    else:
        body_str = body if body else ""

    # Signing message: timestamp + METHOD + full_path (no nonce, no body — matches docs style)
    message = timestamp + method.upper() + full_path

    signature = sign_pss_text(private_key, message)

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

# Fetch markets expiring in next 12 hours (fast execution)
def fetch_soon_closing_markets(hours=12, max_pages=10):
    current_seconds = int(time.time())
    max_close_seconds = current_seconds + (hours * 3600)

    params = {
        "limit": 1000,
        "status": "open",
        "min_close_ts": current_seconds,
        "max_close_ts": max_close_seconds
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
                logger.warning("Rate limit hit — sleeping 60s")
                time.sleep(60)
                break
            raise
        markets_page = data.get('markets', [])
        all_markets.extend(markets_page)
        cursor = data.get('cursor')
        logger.info(f"Page {page}: {len(markets_page)} soon-expiring markets")
        page += 1
        if not cursor:
            break

    return all_markets

VOLUME_THRESHOLD = 5000

def main_loop():
    global daily_loss
    if daily_loss >= DAILY_LOSS_LIMIT:
        logger.warning("Daily loss limit reached — skipping trades today")
        return

    try:
        logger.info("Fetching markets expiring in the next 12 hours...")
        markets = fetch_soon_closing_markets(hours=12, max_pages=10)

        total = len(markets)
        logger.info(f"Fetched {total} markets expiring in next 12 hours")

        if total == 0:
            logger.info("No markets expiring in next 12h — skipping cycle")
            return

        # Sort descending by volume
        markets.sort(key=lambda x: max(x.get('volume', 0), x.get('volume_24h', 0)), reverse=True)

        # Debug: show first 5 markets
        logger.info("First 5 soon-expiring markets:")
        for market in markets[:5]:
            t = market.get('ticker', '??')
            title = market.get('title', 'No title')[:90]
            v = market.get('volume', 0)
            v24 = market.get('volume_24h', 0)
            close_time = market.get('close_time', 'N/A')
            logger.info(f"  → {t} | vol {v:,} / 24h {v24:,} | closes {close_time} | {title}")

        considered = 0
        decided_to_trade = 0

        for market in markets:
            ticker = market.get('ticker')
            if not ticker:
                continue

            title = market.get('title') or market.get('event_ticker', 'Unknown')

            yes_bid = market.get('yes_bid', 50) / 100.0
            yes_ask = market.get('yes_ask', 50) / 100.0
            yes_price = (yes_bid + yes_ask) / 2 if yes_bid > 0 and yes_ask > 0 else 0.50

            volume = max(market.get('volume', 0), market.get('volume_24h', 0))

            if volume < VOLUME_THRESHOLD:
                continue

            considered += 1
            logger.success(f"CONSIDERING: {ticker} | vol={volume:,} | mid={yes_price:.3f} | {title[:80]}")

            decision = should_trade({
                'ticker': ticker,
                'title': title,
                'yes_price': yes_price,
                'description': market.get('description') or market.get('subtitle', '')
            })

            if decision:
                decided_to_trade += 1
                logger.success(f"DECISION TO TRADE: {ticker} | {decision['direction']} | size=${decision['size']:.2f} | {decision['reason']}")

                side = "yes" if decision["direction"] == "YES" else "no"
                action = "buy"

                limit_price_cents = int(yes_price * 100)
                count = int(decision["size"] * 100)

                order_body = {
                    "ticker": ticker,
                    "side": side,
                    "action": action,
                    "count": count,
                    "type": "limit",
                    "yes_price": limit_price_cents,
                    "client_order_id": f"life-fund-{int(time.time())}"
                }

                try:
                    order_response = signed_request("POST", "/portfolio/orders", body=order_body)

                    trade_msg = (
                        f"TRADE PLACED: {decision['direction']} on {ticker}\n"
                        f"Size: ${decision['size']:.2f}\n"
                        f"Price: ${yes_price:.2f}\n"
                        f"Reason: {decision['reason']}\n"
                        f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                    )

                    logger.success(trade_msg)

                    play_trade_notification()

                    update_trade_status(ticker, 'OPEN')

                    cursor = conn.cursor()
                    cursor.execute('''
                        INSERT INTO trades (timestamp, market_ticker, direction, size, price, reason, status)
                        VALUES (datetime('now'), ?, ?, ?, ?, ?, 'OPEN')
                    ''', (ticker, decision['direction'], decision['size'], yes_price, decision['reason']))
                    conn.commit()

                except Exception as order_err:
                    logger.error(f"Order placement failed for {ticker}: {order_err}")

            else:
                logger.info(f"   → No trade decision for {ticker}")

        logger.info(f"Cycle summary | expiring (12h): {total} | considered: {considered} | trades: {decided_to_trade}")

    except Exception as e:
        logger.error(f"Main loop error: {e}")

schedule.every(2).minutes.do(main_loop)

logger.info("🚀 LIFE-SAVING BOT STARTED – KALSHI-ACCESS-* headers + /portfolio/orders")

while True:
    try:
        schedule.run_pending()
        time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        break
    except Exception as e:
        logger.error(f"Crash: {e} — restarting in 30s...")
        time.sleep(30)