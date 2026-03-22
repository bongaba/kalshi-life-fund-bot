import time
import sqlite3
import schedule
import requests
import uuid
import base64
import json
from loguru import logger
from logging_setup import setup_log_file
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from config import *
from decision_engine import should_trade
import winsound

setup_log_file("bot.log")

# Startup debug
logger.info(f"[BOT START] MODE: {MODE}")
logger.info(f"[BOT START] RISK_PER_TRADE: {RISK_PER_TRADE}")
logger.info(f"[BOT START] XAI_API_KEY (first 5 chars): {XAI_API_KEY[:5] if XAI_API_KEY else 'None'}...")

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
    status TEXT DEFAULT 'OPEN'
)''')
conn.commit()

daily_loss = 0.0
daily_trade_count = 0

def daily_pnl_reset():
    global daily_loss, daily_trade_count
    daily_loss = 0.0
    daily_trade_count = 0
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

def update_resolved_trades():
    global daily_loss
    try:
        logger.info("Checking for resolved trades...")
        closed_markets = fetch_closed_markets(days=7, max_pages=5)
        
        for market in closed_markets:
            ticker = market.get('ticker')
            if not ticker:
                continue
            
            settlement_value = market.get('settlement_value')
            if settlement_value is None:
                continue  # Not settled yet
            
            winner = "YES" if settlement_value == 1 else "NO"
            logger.info(f"Market {ticker} resolved: {winner} won")
            
            # Get all open trades for this ticker
            cursor = conn.cursor()
            cursor.execute("SELECT id, direction, size FROM trades WHERE market_ticker = ? AND status = 'OPEN'", (ticker,))
            open_trades = cursor.fetchall()
            
            for trade_id, direction, size in open_trades:
                if direction == winner:
                    pnl = size
                    status = 'WON'
                else:
                    pnl = -size
                    status = 'LOST'
                    daily_loss += size  # Accumulate loss
                
                # Update the trade
                cursor.execute("UPDATE trades SET status = ?, pnl = ? WHERE id = ?", (status, pnl, trade_id))
                logger.info(f"Trade {trade_id} on {ticker}: {status} (${pnl:.2f})")
            
            conn.commit()
            
    except Exception as e:
        logger.error(f"Error updating resolved trades: {e}")

def create_signature(private_key, timestamp, method, path):
    """Create Kalshi request signature - official style"""
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

    # Full relative path for URL (includes /trade-api/v2)
    full_path = "/trade-api/v2" + path
    if params:
        full_path += "?" + "&".join(f"{k}={v}" for k, v in params.items())

    # Path for signing: full relative path without query params
    sign_path = ("/trade-api/v2" + path).split('?')[0]

    # Message exactly as in official example
    message = f"{timestamp}{method.upper()}{sign_path}"

    logger.debug(f"Signing message: {message}")
    logger.debug(f"Full request URL: {host}{full_path}")

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

# Fetch markets expiring in next 12 hours
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

# Fetch recently closed markets (last 7 days)
def fetch_closed_markets(days=7, max_pages=5):
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
                logger.warning("Rate limit hit — sleeping 60s")
                time.sleep(60)
                break
            raise
        markets_page = data.get('markets', [])
        all_markets.extend(markets_page)
        cursor = data.get('cursor')
        logger.info(f"Page {page}: {len(markets_page)} closed markets")
        page += 1
        if not cursor:
            break

    return all_markets

def main_loop():
    global daily_loss, daily_trade_count
    if daily_loss >= DAILY_LOSS_LIMIT:
        logger.warning("Daily loss limit reached — skipping trades today")
        return

    if daily_trade_count >= MAX_TRADES_PER_DAY:
        logger.warning("Daily trade limit reached — skipping trades today")
        return

    try:
        logger.info("Fetching markets expiring in the next 12 hours...")
        markets = fetch_soon_closing_markets(hours=12, max_pages=10)

        total = len(markets)
        logger.info(f"Fetched {total} markets expiring in next 12 hours")

        if total == 0:
            logger.info("No markets expiring in next 12h — skipping cycle")
            return

        markets.sort(key=lambda x: max(x.get('volume', 0), x.get('volume_24h', 0)), reverse=True)

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

            # Check if we already have an open trade on this market
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM trades WHERE market_ticker = ? AND status = 'OPEN'", (ticker,))
            if cursor.fetchone()[0] > 0:
                logger.info(f"   → Already have open trade on {ticker} — skipping")
                continue

            decision = should_trade({
                'ticker': ticker,
                'title': title,
                'yes_price': yes_price,
                'description': market.get('description') or market.get('subtitle', ''),
                'volume': volume,
                'close_time': market.get('close_time')
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
                    "client_order_id": f"life-fund-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
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
                        INSERT INTO trades (timestamp, market_ticker, direction, size, price, pnl, reason, status)
                        VALUES (datetime('now'), ?, ?, ?, ?, 0.0, ?, 'OPEN')
                    ''', (ticker, decision['direction'], decision['size'], yes_price, decision['reason']))
                    conn.commit()

                    daily_trade_count += 1

                except Exception as order_err:
                    response = getattr(order_err, 'response', None)
                    status_code = getattr(response, 'status_code', 'unknown')
                    response_body = (getattr(response, 'text', '') or '')[:1000]
                    logger.error(
                        f"Order placement failed for {ticker}: {order_err} | "
                        f"status={status_code} | body={response_body or 'N/A'}"
                    )

            else:
                logger.info(f"   → No trade decision for {ticker}")

        update_resolved_trades()

        logger.info(f"Cycle summary | expiring (12h): {total} | considered: {considered} | trades: {decided_to_trade}")

    except Exception as e:
        logger.error(f"Main loop error: {e}")

schedule.every(1).minutes.do(main_loop)

logger.info("🚀 Kalshi Trading Bot started – Official Kalshi example signing")

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