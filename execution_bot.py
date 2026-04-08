import time
import sqlite3
import schedule
import requests
import uuid
import base64
import json
import threading
from datetime import datetime, timezone
from loguru import logger
from logging_setup import setup_log_file, setup_error_log, setup_trade_decision_log
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from config import *
from decision_engine import should_trade
from discord_notifications import notify_trade_executed, notify_error, notify_startup, notify_cycle_summary, notify_account_balance
import discord_bot
import winsound

setup_log_file("bot.log")
setup_error_log()
setup_trade_decision_log()


def get_decision_mode_label() -> str:
    if OVERRIDE_INTERNAL_MODEL_WITH_GROK:
        if OVERRIDE_GROK_IGNORE_VOLUME_GATE:
            return "grok_override_with_volume_bypass"
        return "grok_override"
    return "internal_model_plus_validators"


def get_dynamic_loop_interval_minutes() -> int:
    """Return loop interval from BOT_LOOP_SCHEDULE (required)."""
    now = datetime.now().time()

    def parse_hhmm(raw: str):
        hour, minute = raw.split(":", 1)
        hour_int = int(hour)
        minute_int = int(minute)
        if not (0 <= hour_int <= 23 and 0 <= minute_int <= 59):
            raise ValueError(f"Invalid time '{raw}'")
        return hour_int, minute_int

    def is_in_window(current_time, start_hhmm: str, end_hhmm: str) -> bool:
        start_h, start_m = parse_hhmm(start_hhmm)
        end_h, end_m = parse_hhmm(end_hhmm)

        current_minutes = current_time.hour * 60 + current_time.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        if start_minutes < end_minutes:
            return start_minutes <= current_minutes < end_minutes
        return current_minutes >= start_minutes or current_minutes < end_minutes

    def parse_schedule(schedule_text: str):
        windows = []
        for chunk in schedule_text.split(","):
            item = chunk.strip()
            if not item:
                continue

            range_part, interval_part = item.split("=", 1)
            start_part, end_part = range_part.split("-", 1)

            start_part = start_part.strip()
            end_part = end_part.strip()
            interval_minutes = int(interval_part.strip())
            if interval_minutes <= 0:
                raise ValueError("Interval must be > 0")

            parse_hhmm(start_part)
            parse_hhmm(end_part)
            windows.append((start_part, end_part, interval_minutes))
        return windows

    if not BOT_LOOP_SCHEDULE:
        raise ValueError("BOT_LOOP_SCHEDULE must be set in .env")

    windows = parse_schedule(BOT_LOOP_SCHEDULE)
    if not windows:
        raise ValueError("BOT_LOOP_SCHEDULE is empty after parsing")

    for start_hhmm, end_hhmm, interval_minutes in windows:
        if is_in_window(now, start_hhmm, end_hhmm):
            return interval_minutes

    raise ValueError(
        f"No BOT_LOOP_SCHEDULE window matched current time {now.strftime('%H:%M')}"
    )

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
conn = sqlite3.connect('trades.db', timeout=5)
conn.execute("PRAGMA busy_timeout = 5000")
conn.execute("PRAGMA journal_mode=WAL")
conn.execute('''CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    market_ticker TEXT,
    direction TEXT,
    size REAL,
    price REAL,
    pnl REAL DEFAULT 0,
    reason TEXT,
    status TEXT DEFAULT 'OPEN',
    client_order_id TEXT,
    kalshi_order_id TEXT,
    order_status TEXT
)''')
conn.commit()

def ensure_trade_table_columns():
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(trades)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    required_columns = {
        'client_order_id': 'TEXT',
        'kalshi_order_id': 'TEXT',
        'order_status': 'TEXT',
        'resolved_timestamp': 'TEXT',
        'fees': 'REAL DEFAULT 0',
        'event_ticker': 'TEXT',
    }

    for column_name, column_type in required_columns.items():
        if column_name not in existing_columns:
            cursor.execute(f"ALTER TABLE trades ADD COLUMN {column_name} {column_type}")

    conn.commit()

ensure_trade_table_columns()

daily_loss = 0.0
daily_trade_count = 0
_DAILY_LOCK = threading.Lock()

def daily_pnl_reset():
    global daily_loss, daily_trade_count
    with _DAILY_LOCK:
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

def fetch_fill_price_and_fees(order_id, side):
    """Fetch actual fill price and fees from Kalshi fills endpoint.

    Returns (avg_fill_price, total_fees) or (None, 0.0) on failure.
    """
    try:
        resp = signed_request('GET', '/portfolio/fills', params={'order_id': order_id})
        fills = resp.get('fills', [])
        if not fills:
            return None, 0.0

        total_value = 0.0
        total_count = 0.0
        total_fees = 0.0

        for fill in fills:
            count = float(fill.get('count_fp', 0))
            if side == 'no':
                price = float(fill.get('no_price_dollars', 0))
            else:
                price = float(fill.get('yes_price_dollars', 0))
            total_value += price * count
            total_count += count
            total_fees += float(fill.get('fee_cost', 0))

        if total_count > 0:
            return total_value / total_count, total_fees
        return None, 0.0
    except Exception as e:
        logger.warning(f"Failed to fetch fills for order {order_id}: {e}")
        return None, 0.0


def extract_order_metadata(order_response):
    if not isinstance(order_response, dict):
        return None, None

    order_data = order_response.get('order')
    if not isinstance(order_data, dict):
        order_data = order_response

    order_id = order_data.get('order_id') or order_data.get('id')
    order_status = order_data.get('status') or order_response.get('status')
    return order_id, order_status

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


def resolve_winner_from_settlement_value(raw_value):
    """Normalize Kalshi settlement values and return YES/NO winner or None if unresolved."""
    if raw_value is None:
        return None

    # Handle strings such as "1", "0", "100", "yes", "no".
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"yes", "y", "true", "won_yes"}:
            return "YES"
        if normalized in {"no", "n", "false", "won_no"}:
            return "NO"
        try:
            raw_value = float(normalized)
        except ValueError:
            return None

    # Kalshi payloads can be 0/1 or 0/100 style; treat >= 0.5 (or >= 50) as YES.
    if isinstance(raw_value, (int, float)):
        value = float(raw_value)
        if value in (0.0, 1.0):
            return "YES" if value == 1.0 else "NO"
        if value in (0.0, 100.0):
            return "YES" if value == 100.0 else "NO"
        if value > 1.0:
            return "YES" if value >= 50.0 else "NO"
        return "YES" if value >= 0.5 else "NO"

    return None


def resolve_winner_from_market_payload(market):
    """Resolve YES/NO winner from documented market fields with compatibility fallbacks."""
    result = market.get('result')
    if isinstance(result, str):
        normalized = result.strip().lower()
        if normalized in {'yes', 'y'}:
            return 'YES'
        if normalized in {'no', 'n'}:
            return 'NO'

    # Fallback for older/alternate payload conventions.
    settlement_value = market.get('settlement_value')
    if settlement_value is None:
        settlement_value = market.get('settlement_value_dollars')
    return resolve_winner_from_settlement_value(settlement_value)


def resolve_winner_from_settlement_payload(settlement):
    """Resolve YES/NO winner from documented portfolio settlement payload."""
    market_result = settlement.get('market_result')
    if isinstance(market_result, str):
        normalized = market_result.strip().lower()
        if normalized in {'yes', 'y'}:
            return 'YES'
        if normalized in {'no', 'n'}:
            return 'NO'
    return None


def normalize_settled_timestamp(settled_time_raw):
    """Convert API settled_time to SQLite-compatible UTC timestamp string."""
    if not settled_time_raw:
        return None

    if isinstance(settled_time_raw, str):
        raw = settled_time_raw.strip()
        try:
            if raw.endswith('Z'):
                dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            else:
                dt = datetime.fromisoformat(raw)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt.strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            return None

    return None


def fetch_portfolio_settlements(days=30, max_pages=20):
    min_ts = int(time.time()) - (days * 24 * 3600)
    params = {
        "limit": 200,
        "min_ts": min_ts,
    }

    all_settlements = []
    cursor = None
    page = 1

    while page <= max_pages:
        if cursor:
            params["cursor"] = cursor
        try:
            data = signed_request("GET", "/portfolio/settlements", params=params)
        except requests.exceptions.HTTPError as e:
            if e.response and e.response.status_code == 429:
                logger.warning("Rate limit hit on settlements endpoint — sleeping 60s")
                time.sleep(60)
                break
            raise

        settlements_page = data.get('settlements', [])
        all_settlements.extend(settlements_page)
        cursor = data.get('cursor')
        logger.info(f"Page {page}: {len(settlements_page)} portfolio settlements")
        page += 1
        if not cursor:
            break

    return all_settlements

def update_resolved_trades():
    global daily_loss
    try:
        logger.info("Checking for resolved trades...")
        settlements = fetch_portfolio_settlements(days=30, max_pages=CLOSED_MARKETS_MAX_PAGES)

        for settlement in settlements:
            ticker = settlement.get('ticker')
            if not ticker:
                continue
            
            winner = resolve_winner_from_settlement_payload(settlement)
            if winner is None:
                continue  # Not settled yet

            settled_timestamp = normalize_settled_timestamp(settlement.get('settled_time'))
            if settled_timestamp is None:
                settled_timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

            logger.info(
                f"Settlement {ticker}: {winner} won | market_result={settlement.get('market_result')} | "
                f"settled_time={settlement.get('settled_time')}"
            )
            
            # Get all open/pending trades for this ticker (OPEN, CLOSED by monitor, or SETTLED by reconciliation)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, direction, size, status FROM trades WHERE market_ticker = ? AND status IN ('OPEN', 'CLOSED', 'SETTLED')",
                (ticker,),
            )
            open_trades = cursor.fetchall()
            
            for trade_id, direction, size, current_status in open_trades:
                if direction == winner:
                    pnl = size
                    status = 'WON'
                else:
                    pnl = -size
                    status = 'LOST'
                    if current_status == 'OPEN':
                        daily_loss += size  # Only accumulate loss for positions not already exited
                
                # Update the trade
                cursor.execute(
                    "UPDATE trades SET status = ?, pnl = ?, resolved_timestamp = ? WHERE id = ?",
                    (status, pnl, settled_timestamp, trade_id),
                )
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
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }
    if body:
        headers["Content-Type"] = "application/json"

    url = f"{host}{full_path}"

    if method.upper() == "GET":
        response = requests.get(url, headers=headers, timeout=10)
    elif method.upper() == "POST":
        response = requests.post(url, headers=headers, json=body, timeout=10)
    else:
        raise ValueError(f"Unsupported method: {method}")

    response.raise_for_status()
    return response.json()

def log_account_balance():
    try:
        data = signed_request("GET", "/portfolio/balance")
        balance = data.get('balance', 0) / 100
        portfolio_value = data.get('portfolio_value', 0) / 100
        total_value = balance + portfolio_value

        logger.info(
            f"Account balance | cash: ${balance:.2f} | portfolio: ${portfolio_value:.2f} | total: ${total_value:.2f}"
        )
    except Exception as balance_err:
        logger.error(f"Balance check failed: {balance_err}")

def market_volume_value(market, *field_names):
    for field_name in field_names:
        value = market.get(field_name)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0

def get_market_volumes(market):
    total_volume = market_volume_value(market, 'volume', 'volume_fp')
    volume_24h = market_volume_value(market, 'volume_24h', 'volume_24h_fp')
    return total_volume, volume_24h, max(total_volume, volume_24h)

def market_price_value(market, *field_names, divisor=1.0):
    for field_name in field_names:
        value = market.get(field_name)
        if value in (None, ""):
            continue
        try:
            return float(value) / divisor
        except (TypeError, ValueError):
            continue
    return None

def normalize_market_price(price):
    if price is None:
        return None
    if price > 1:
        price /= 100.0
    return price

def complementary_market_price(price):
    if price is None:
        return None
    complement = 1.0 - price
    if 0.0 <= complement <= 1.0:
        return complement
    return None

def midpoint_price(bid_price, ask_price):
    if bid_price is not None and ask_price is not None and bid_price > 0 and ask_price > 0:
        return (bid_price + ask_price) / 2
    return None


def is_multivariate_market(market):
    ticker = str(market.get('ticker') or '')
    return bool(market.get('mve_collection_ticker')) or ticker.startswith('KXMVECROSSCATEGORY-')

def get_market_prices(market):
    yes_bid = market_price_value(market, 'yes_bid_dollars', 'yes_bid')
    yes_ask = market_price_value(market, 'yes_ask_dollars', 'yes_ask')
    no_bid = market_price_value(market, 'no_bid_dollars', 'no_bid')
    no_ask = market_price_value(market, 'no_ask_dollars', 'no_ask')
    last_yes_price = market_price_value(market, 'last_price_dollars', 'last_price')
    previous_yes_price = market_price_value(market, 'previous_price_dollars', 'previous_price')

    yes_bid = normalize_market_price(yes_bid)
    yes_ask = normalize_market_price(yes_ask)
    no_bid = normalize_market_price(no_bid)
    no_ask = normalize_market_price(no_ask)
    last_yes_price = normalize_market_price(last_yes_price)
    previous_yes_price = normalize_market_price(previous_yes_price)

    if yes_bid is None and no_ask is not None:
        yes_bid = complementary_market_price(no_ask)
    if yes_ask is None and no_bid is not None:
        yes_ask = complementary_market_price(no_bid)
    if no_bid is None and yes_ask is not None:
        no_bid = complementary_market_price(yes_ask)
    if no_ask is None and yes_bid is not None:
        no_ask = complementary_market_price(yes_bid)

    yes_price = midpoint_price(yes_bid, yes_ask)
    no_price = midpoint_price(no_bid, no_ask)

    if yes_price is None:
        yes_price = last_yes_price or previous_yes_price
    if no_price is None and yes_price is not None:
        no_price = complementary_market_price(yes_price)

    if yes_price is None and no_price is not None:
        yes_price = complementary_market_price(no_price)
    if no_price is None and yes_price is not None:
        no_price = complementary_market_price(yes_price)

    return {
        'yes_bid': yes_bid,
        'yes_ask': yes_ask,
        'yes_price': yes_price,
        'no_bid': no_bid,
        'no_ask': no_ask,
        'no_price': no_price,
    }

# Fetch markets expiring in the configured scan window
def fetch_soon_closing_markets(hours=MARKET_SCAN_HOURS, max_pages=OPEN_MARKETS_MAX_PAGES):
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
            if e.response is not None and e.response.status_code == 429:
                logger.warning("Rate limit hit fetching open markets — sleeping 60s then retrying")
                time.sleep(60)
                continue  # retry same page
            raise
        markets_page = data.get('markets', [])
        all_markets.extend(markets_page)
        cursor = data.get('cursor')
        logger.info(f"Page {page}: {len(markets_page)} soon-expiring markets")
        page += 1
        if not cursor:
            break

    return all_markets

# Fetch recently settled markets (last N days)
def fetch_closed_markets(days=7, max_pages=CLOSED_MARKETS_MAX_PAGES):
    current_seconds = int(time.time())
    min_settled_seconds = current_seconds - (days * 24 * 3600)

    params = {
        "limit": 1000,
        "status": "settled",
        "min_settled_ts": min_settled_seconds
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
            if e.response is not None and e.response.status_code == 429:
                logger.warning("Rate limit hit fetching settled markets — sleeping 60s then retrying")
                time.sleep(60)
                continue  # retry same page
            raise
        markets_page = data.get('markets', [])
        all_markets.extend(markets_page)
        cursor = data.get('cursor')
        logger.info(f"Page {page}: {len(markets_page)} settled markets")
        page += 1
        if not cursor:
            break

    return all_markets

def main_loop():
    global daily_loss, daily_trade_count
    try:
        # Check Discord remote pause flag
        if discord_bot.is_paused("execution"):
            logger.info("[BOT CYCLE] Execution bot is PAUSED via Discord — skipping cycle")
            return

        logger.info(
            f"[BOT CYCLE] Decision mode={get_decision_mode_label()} | use_grok={USE_GROK} | "
            f"override_internal_model_with_grok={OVERRIDE_INTERNAL_MODEL_WITH_GROK} | "
            f"override_grok_ignore_volume_gate={OVERRIDE_GROK_IGNORE_VOLUME_GATE}"
        )

        if daily_loss >= DAILY_LOSS_LIMIT:
            logger.warning("Daily loss limit reached — skipping trades today")
            return

        if daily_trade_count >= MAX_TRADES_PER_DAY:
            logger.warning("Daily trade limit reached — skipping trades today")
            return

        try:
            balance_data = signed_request("GET", "/portfolio/balance")
            cash = balance_data.get('balance', 0) / 100
            portfolio_value = balance_data.get('portfolio_value', 0) / 100
            total_value = cash + portfolio_value
            cash_ratio = cash / total_value if total_value > 0 else 0.0
            logger.info(
                f"[BALANCE CHECK] cash=${cash:.2f} | portfolio=${portfolio_value:.2f} | "
                f"total=${total_value:.2f} | cash_ratio={cash_ratio:.2%} | min_required={MIN_CASH_RATIO:.2%}"
            )
            if cash_ratio < MIN_CASH_RATIO:
                logger.warning(
                    f"[BALANCE CHECK] Skipping cycle — cash ratio {cash_ratio:.2%} is below minimum {MIN_CASH_RATIO:.2%}"
                )
                return
        except Exception as balance_err:
            logger.error(f"[BALANCE CHECK] Failed to fetch balance: {balance_err} — skipping cycle for safety")
            return

        logger.info(f"Fetching markets expiring in the next {MARKET_SCAN_HOURS} hours...")
        markets = fetch_soon_closing_markets(max_pages=OPEN_MARKETS_MAX_PAGES)

        if EXCLUDED_MARKET_TICKERS:
            excluded_tickers = set(EXCLUDED_MARKET_TICKERS)
            before_count = len(markets)
            markets = [
                market
                for market in markets
                if str(market.get('ticker') or '').upper() not in excluded_tickers
            ]
            logger.info(
                f"Applied ticker exclusion filter | excluded={sorted(excluded_tickers)} | before={before_count} | after={len(markets)}"
            )

        if MARKET_TITLE_CONTAINS:
            filter_terms = [t.strip().lower() for t in MARKET_TITLE_CONTAINS.split(',') if t.strip()]
            before_count = len(markets)
            markets = [
                market
                for market in markets
                if any(term in str(market.get('title') or '').lower() for term in filter_terms)
            ]
            logger.info(
                f"Applied title filter | contains={filter_terms} | before={before_count} | after={len(markets)}"
            )

        total = len(markets)
        logger.info(f"Fetched {total} markets expiring in next {MARKET_SCAN_HOURS} hours")

        if total == 0:
            logger.info(f"No markets expiring in next {MARKET_SCAN_HOURS}h — skipping cycle")
            return

        markets.sort(key=lambda market: get_market_volumes(market)[2], reverse=True)

        logger.info("First 5 soon-expiring markets:")
        for market in markets[:5]:
            t = market.get('ticker', '??')
            title = market.get('title', 'No title')[:90]
            v, v24, _ = get_market_volumes(market)
            close_time = market.get('close_time', 'N/A')
            logger.info(f"  → {t} | vol {v:,.0f} / 24h {v24:,.0f} | closes {close_time} | {title}")

        considered = 0
        decided_to_trade = 0
        cycle_total_order_cost = 0.0

        # Load per-event trade counts to enforce MAX_TRADES_PER_EVENT
        MAX_TRADES_PER_EVENT = 2
        cursor = conn.cursor()
        cursor.execute(
            "SELECT event_ticker, COUNT(*) FROM trades "
            "WHERE event_ticker IS NOT NULL AND status != 'CANCELED' "
            "GROUP BY event_ticker HAVING COUNT(*) >= ?",
            (MAX_TRADES_PER_EVENT,),
        )
        saturated_events = {row[0] for row in cursor.fetchall()}
        # Also track events traded this cycle (in case multiple markets from same event appear)
        cycle_event_trades = {}  # event_ticker -> count this cycle

        for market in markets:
            ticker = market.get('ticker')
            if not ticker:
                continue

            if is_multivariate_market(market):
                logger.info(f"   → Skipping {ticker}: multivariate/combo market without reliable binary pricing")
                continue

            event_ticker = market.get('event_ticker') or ''
            title = market.get('title') or event_ticker or 'Unknown'

            # Skip if this event already has MAX_TRADES_PER_EVENT trades (DB + this cycle)
            if event_ticker:
                db_count = 0
                if event_ticker in saturated_events:
                    db_count = MAX_TRADES_PER_EVENT
                else:
                    db_count = cursor.execute(
                        "SELECT COUNT(*) FROM trades WHERE event_ticker = ? AND status != 'CANCELED'",
                        (event_ticker,),
                    ).fetchone()[0]
                total_count = db_count + cycle_event_trades.get(event_ticker, 0)
                if total_count >= MAX_TRADES_PER_EVENT:
                    logger.info(f"   → Skipping {ticker}: event {event_ticker} already has {total_count} trade(s) (limit={MAX_TRADES_PER_EVENT})")
                    continue

            price_data = get_market_prices(market)
            yes_price = price_data['yes_price']
            no_price = price_data['no_price']

            if yes_price is None or no_price is None:
                logger.info(
                    "   → Skipping {}: could not derive YES/NO prices | yes_bid={} | yes_ask={} | no_bid={} | no_ask={} | last_yes={} | prev_yes={}",
                    ticker,
                    price_data['yes_bid'],
                    price_data['yes_ask'],
                    price_data['no_bid'],
                    price_data['no_ask'],
                    normalize_market_price(market_price_value(market, 'last_price_dollars', 'last_price')),
                    normalize_market_price(market_price_value(market, 'previous_price_dollars', 'previous_price')),
                )
                continue

            _, _, volume = get_market_volumes(market)

            if volume < VOLUME_THRESHOLD:
                continue

            considered += 1
            logger.success(f"CONSIDERING: {ticker} | vol={volume:,.0f} | mid={yes_price:.3f} | {title[:80]}")

            # Check if we already have an open trade on this market
            # cursor = conn.cursor()
            # cursor.execute("SELECT COUNT(*) FROM trades WHERE market_ticker = ? AND status = 'OPEN'", (ticker,))
            # if cursor.fetchone()[0] > 0:
            #     logger.info(f"   → Already have open trade on {ticker} — skipping")
            #     continue

            decision = should_trade({
                'ticker': ticker,
                'title': title,
                'yes_price': yes_price,
                'no_price': no_price,
                'description': market.get('description') or market.get('subtitle', ''),
                'volume': volume,
                'close_time': market.get('close_time')
            })

            if decision:
                decided_to_trade += 1
                logger.success(f"[DECISION] TRADE: {ticker} | {decision['direction']} | conf={decision['confidence']}% | size=${decision['size']:.2f} | {decision['reason']}")

                side = "yes" if decision["direction"] == "YES" else "no"
                action = "buy"

                # Use the ASK price (what sellers are offering) so IOC orders
                # actually match resting liquidity.  Fall back to midpoint if
                # no ask is available, and add 1¢ slippage to handle tiny
                # orderbook movements between quote and order submission.
                if side == "yes":
                    contract_price = price_data.get('yes_ask') or yes_price
                else:
                    contract_price = price_data.get('no_ask') or no_price

                SLIPPAGE_CENTS = 1  # 1¢ slippage tolerance
                limit_price_cents = min(99, int(contract_price * 100) + SLIPPAGE_CENTS)  # cap at 99¢ (Kalshi max)
                limit_price_dollars = limit_price_cents / 100.0
                # Number of contracts to buy, derived from the size computed in the decision engine.
                count = int(decision["size"] * 100)

                # Ultra-high probability: allocate 50% of available cash instead of normal sizing
                if decision.get("half_cash_sizing") and limit_price_dollars > 0:
                    half_cash_count = int((cash * 0.50) / limit_price_dollars)
                    if half_cash_count > count:
                        logger.info(
                            f"[SIZING] Ultra-high probability override for {ticker}: "
                            f"normal={count} contracts → 50% cash={half_cash_count} contracts "
                            f"(cash=${cash:.2f}, limit=${limit_price_dollars:.4f})"
                        )
                        count = half_cash_count

                total_order_cost = count * limit_price_dollars

                client_order_id = f"life-fund-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

                order_body = {
                    "ticker": ticker,
                    "side": side,
                    "action": action,
                    "count": count,
                    "type": "limit",
                    "time_in_force": "immediate_or_cancel",
                    "client_order_id": client_order_id,
                }
                if side == "yes":
                    order_body["yes_price"] = limit_price_cents
                else:
                    order_body["no_price"] = limit_price_cents

                try:
                    order_response = signed_request("POST", "/portfolio/orders", body=order_body)
                    kalshi_order_id, order_status = extract_order_metadata(order_response)

                    order_data_resp = order_response.get('order', order_response) if isinstance(order_response, dict) else {}
                    fill_count_fp = float(order_data_resp.get('fill_count_fp', 0) or 0)
                    actually_filled = fill_count_fp > 0 and order_status != 'canceled'
                    db_status = 'OPEN' if actually_filled else 'CANCELED'

                    logger.success(
                        f"[ORDER] ACCEPTED: {ticker} | client_order_id={client_order_id} | "
                        f"kalshi_order_id={kalshi_order_id or 'unknown'} | status={order_status or 'unknown'} | "
                        f"filled={fill_count_fp} | db_status={db_status}"
                    )
                    logger.debug(f"Order response for {ticker}: {json.dumps(order_response)}")

                    if not actually_filled:
                        logger.warning(f"IOC order for {ticker} was NOT filled (status={order_status}). Skipping DB insert.")
                    else:
                        # Fetch actual fill price and fees from exchange
                        actual_price, entry_fees = fetch_fill_price_and_fees(kalshi_order_id, side)
                        if actual_price is not None:
                            if abs(actual_price - contract_price) > 0.001:
                                logger.info(
                                    f"Fill price differs from limit: submitted ${contract_price:.4f} → "
                                    f"filled ${actual_price:.4f} (improvement: ${contract_price - actual_price:+.4f})"
                                )
                            contract_price = actual_price
                            total_order_cost = count * contract_price
                        else:
                            entry_fees = 0.0
                            logger.warning(f"Could not fetch fills for {ticker}, using limit price ${contract_price:.4f}")

                        trade_msg = (
                            f"[TRADE] EXECUTED: {decision['direction']} on {ticker}\n"
                            f"Quantity: {count}\n"
                            f"Price: ${contract_price:.4f}\n"
                            f"Total Cost: ${total_order_cost:.2f}\n"
                            f"Fees: ${entry_fees:.2f}\n"
                            f"Reason: {decision['reason']}\n"
                            f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                        )

                        logger.success(trade_msg)

                        play_trade_notification()

                        # Send Discord trade notification with exchange acknowledgement details.
                        notify_trade_executed(
                            ticker,
                            title,
                            decision['direction'],
                            decision['confidence'],
                            count,
                            contract_price,
                            decision['reason'],
                            total_order_cost,
                            is_undervalued=decision.get('is_undervalued', False),
                            order_status=order_status,
                            fees=entry_fees,
                        )

                        cursor = conn.cursor()
                        cursor.execute('''
                            INSERT INTO trades (
                                timestamp,
                                market_ticker,
                                direction,
                                size,
                                price,
                                pnl,
                                reason,
                                status,
                                client_order_id,
                                kalshi_order_id,
                                order_status,
                                fees,
                                event_ticker
                            )
                            VALUES (datetime('now'), ?, ?, ?, ?, 0.0, ?, 'OPEN', ?, ?, ?, ?, ?)
                        ''', (
                            ticker,
                            decision['direction'],
                            decision['size'],
                            contract_price,
                            decision['reason'],
                            client_order_id,
                            kalshi_order_id,
                            order_status,
                            entry_fees,
                            event_ticker,
                        ))
                        conn.commit()

                        # Track event trades this cycle
                        if event_ticker:
                            cycle_event_trades[event_ticker] = cycle_event_trades.get(event_ticker, 0) + 1

                        cycle_total_order_cost += total_order_cost
                        daily_trade_count += 1

                except Exception as order_err:
                    response = getattr(order_err, 'response', None)
                    status_code = getattr(response, 'status_code', 'unknown')
                    response_body = (getattr(response, 'text', '') or '')[:1000]
                    logger.error(
                        f"Order placement failed for {ticker}: {order_err} | "
                        f"status={status_code} | body={response_body or 'N/A'}"
                    )
                    # Send Discord error notification
                    notify_error(
                        f"Order placement failed for {ticker}: {order_err} | "
                        f"status={status_code} | body={response_body or 'N/A'}"
                    )

            else:
                logger.info(f"[DECISION] SKIP: {ticker} | yes=${yes_price:.3f} | no=${no_price:.3f} | vol={volume:,.0f}")

        update_resolved_trades()

        logger.info(f"Cycle summary | expiring ({MARKET_SCAN_HOURS}h): {total} | considered: {considered} | trades: {decided_to_trade}")

        # Single consolidated Discord notification — only when trades were made
        if decided_to_trade > 0:
            cycle_balance = None
            cycle_portfolio = None
            try:
                bal_data = signed_request("GET", "/portfolio/balance")
                cycle_balance = bal_data.get('balance', 0) / 100
                cycle_portfolio = bal_data.get('portfolio_value', 0) / 100
            except Exception:
                pass

            pnl_today = 0.0
            notify_cycle_summary(total, considered, decided_to_trade, pnl_today, cycle_total_order_cost,
                                 balance=cycle_balance, portfolio_value=cycle_portfolio)

    except Exception as e:
        logger.error(f"Main loop error: {e}")
        # Send Discord error notification
        notify_error(f"Main loop error: {e}")
    finally:
        log_account_balance()

logger.info("🚀 Kalshi Execution Bot started – Official Kalshi example signing")
logger.info(f"[BOT START] LOOP_SCHEDULE: {BOT_LOOP_SCHEDULE}")
logger.info(f"[BOT START] RUN_MODE: {BOT_RUN_MODE}")
logger.info(f"[BOT START] DECISION_MODE: {get_decision_mode_label()}")

# Start Discord command listener for remote control
discord_bot.start_command_listener()

if BOT_RUN_MODE == "single_run":
    logger.info("[BOT START] Single-run mode active | executing one cycle and exiting")
    main_loop()
else:
    # Send Discord startup notification only for long-running daemon mode.
    notify_startup()

    while True:
        try:
            schedule.run_pending()
            main_loop()

            interval_minutes = get_dynamic_loop_interval_minutes()
            sleep_seconds = interval_minutes * 60
            logger.info(
                f"[BOT LOOP] Next cycle in {interval_minutes} minutes (local-time dynamic schedule)"
            )

            slept = 0
            while slept < sleep_seconds:
                schedule.run_pending()
                chunk = min(30, sleep_seconds - slept)
                time.sleep(chunk)
                slept += chunk
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            break
        except Exception as e:
            logger.error(f"Crash: {e} — restarting in 30s...")
            time.sleep(30)