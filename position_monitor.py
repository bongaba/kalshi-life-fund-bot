import time
import sqlite3
import requests
import base64
import json
import uuid
import threading
from loguru import logger
from logging_setup import setup_log_file
from config import *
from discord_notifications import notify_position_closed
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

setup_log_file("monitor.log")

TAKE_PROFIT_PERCENT = POSITION_TAKE_PROFIT_PERCENT
STOP_LOSS_PERCENT = POSITION_STOP_LOSS_PERCENT
MONITOR_INTERVAL_SECONDS = POSITION_MONITOR_INTERVAL_SECONDS
POSITION_CLOSE_COOLDOWN_SECONDS = max(10, MONITOR_INTERVAL_SECONDS * 2)
PENDING_CLOSE_UNTIL = {}

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


def get_market_quotes(ticker):
    """Return best yes/no bid prices in dollars and cents for close order pricing."""
    try:
        data = signed_request("GET", f"/markets/{ticker}")
        market = data.get('market', {})

        yes_bid_cents = int(market.get('yes_bid', 0) or 0)
        no_bid_cents = int(market.get('no_bid', 0) or 0)
        yes_ask_cents = int(market.get('yes_ask', 0) or 0)
        no_ask_cents = int(market.get('no_ask', 0) or 0)

        # If bid is unavailable, use ask as fallback to maximize chance of IOC execution.
        if yes_bid_cents <= 0 and yes_ask_cents > 0:
            yes_bid_cents = yes_ask_cents
        if no_bid_cents <= 0 and no_ask_cents > 0:
            no_bid_cents = no_ask_cents

        yes_bid_cents = min(99, max(1, yes_bid_cents if yes_bid_cents > 0 else 50))
        no_bid_cents = min(99, max(1, no_bid_cents if no_bid_cents > 0 else 50))

        # Compute mark from this same payload to avoid a second /markets call.
        if yes_bid_cents > 0 and yes_ask_cents > 0:
            mark_yes = (yes_bid_cents + yes_ask_cents) / 200.0
        elif yes_bid_cents > 0:
            mark_yes = yes_bid_cents / 100.0
        elif yes_ask_cents > 0:
            mark_yes = yes_ask_cents / 100.0
        else:
            mark_yes = 0.5

        return {
            "yes_bid_cents": yes_bid_cents,
            "no_bid_cents": no_bid_cents,
            "yes_bid": yes_bid_cents / 100.0,
            "no_bid": no_bid_cents / 100.0,
            "mark_yes": mark_yes,
        }
    except Exception as e:
        logger.error(f"Failed to get market quotes for {ticker}: {e}")
        return {
            "yes_bid_cents": 50,
            "no_bid_cents": 50,
            "yes_bid": 0.5,
            "no_bid": 0.5,
            "mark_yes": 0.5,
        }


def get_db_entry_for_ticker(cursor, ticker):
    """Return latest open trade direction and entry price for ticker from local DB."""
    cursor.execute(
        """
        SELECT direction, price
        FROM trades
        WHERE market_ticker = ? AND status = 'OPEN'
        ORDER BY timestamp DESC, id DESC
        LIMIT 1
        """,
        (ticker,),
    )
    row = cursor.fetchone()
    if not row:
        return None, None
    return row[0], float(row[1])


def place_ioc_close_order(ticker, direction, count, quotes):
    """Send sell-to-close IOC order for YES/NO side."""
    side = "yes" if direction == "YES" else "no"
    client_order_id = f"position-close-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

    order_body = {
        "ticker": ticker,
        "action": "sell",
        "side": side,
        "count": int(count),
        "type": "limit",
        "time_in_force": "immediate_or_cancel",
        "client_order_id": client_order_id,
    }

    if side == "yes":
        order_body["yes_price"] = int(quotes["yes_bid_cents"])
    else:
        order_body["no_price"] = int(quotes["no_bid_cents"])

    return signed_request("POST", "/portfolio/orders", body=order_body), order_body


def get_db_connection():
    conn = sqlite3.connect('trades.db', timeout=5)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def notify_position_closed_async(
    ticker,
    direction,
    quantity,
    entry_price,
    exit_price,
    pnl_dollars,
    pnl_percent,
    trigger,
    order_status,
):
    """Dispatch Discord notification on a daemon thread to keep monitor path non-blocking."""
    thread = threading.Thread(
        target=notify_position_closed,
        kwargs={
            "ticker": ticker,
            "direction": direction,
            "quantity": quantity,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl_dollars": pnl_dollars,
            "pnl_percent": pnl_percent,
            "trigger": trigger,
            "order_status": order_status,
        },
        daemon=True,
    )
    thread.start()


def monitor_positions_once():
    """Monitor live positions and submit IOC close orders at configured P&L % thresholds."""
    logger.info("Checking open positions for take-profit/stop-loss exits...")

    positions = get_current_positions()
    if not positions:
        logger.info("No open positions found")
        return

    conn = get_db_connection()
    cursor = conn.cursor()

    for position in positions:
        ticker = position.get('ticker')
        raw_position = position.get('position', 0)

        try:
            contracts = int(abs(float(raw_position)))
        except (TypeError, ValueError):
            contracts = 0

        if not ticker or contracts <= 0:
            continue

        api_direction = "YES" if float(raw_position) > 0 else "NO"
        db_direction, entry_price = get_db_entry_for_ticker(cursor, ticker)
        if entry_price is None:
            logger.info(f"Skipping {ticker}: no OPEN entry row found in trades.db")
            continue

        direction = db_direction if db_direction in {"YES", "NO"} else api_direction
        quotes = get_market_quotes(ticker)
        current_yes = float(quotes["mark_yes"])
        current_no = 1.0 - current_yes
        current_price = current_yes if direction == "YES" else current_no

        unrealized_pnl = contracts * (current_price - entry_price)
        unrealized_pnl_pct = ((current_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0

        logger.info(
            f"{ticker} | dir={direction} | contracts={contracts} | entry={entry_price:.3f} | "
            f"mark={current_price:.3f} | unrealized_pnl=${unrealized_pnl:.2f} | pnl_pct={unrealized_pnl_pct:.2f}%"
        )

        should_take_profit = unrealized_pnl_pct >= TAKE_PROFIT_PERCENT
        should_stop_loss = unrealized_pnl_pct <= STOP_LOSS_PERCENT

        if not should_take_profit and not should_stop_loss:
            continue

        trigger = "take_profit" if should_take_profit else "stop_loss"
        close_key = f"{ticker}:{direction}"
        now_ts = time.time()
        pending_until = PENDING_CLOSE_UNTIL.get(close_key, 0.0)
        if now_ts < pending_until:
            logger.info(
                f"Cooldown active for {ticker} {direction}: skipping duplicate close for {pending_until - now_ts:.1f}s"
            )
            continue

        logger.warning(
            f"Exit trigger hit for {ticker}: {trigger} | pnl=${unrealized_pnl:.2f} | pnl_pct={unrealized_pnl_pct:.2f}% | "
            f"thresholds_pct=({TAKE_PROFIT_PERCENT:.2f}%/{STOP_LOSS_PERCENT:.2f}%)"
        )

        try:
            PENDING_CLOSE_UNTIL[close_key] = now_ts + POSITION_CLOSE_COOLDOWN_SECONDS
            response, order_body = place_ioc_close_order(ticker, direction, contracts, quotes)
            order_data = response.get("order", {}) if isinstance(response, dict) else {}
            order_status = order_data.get("status") or response.get("status") if isinstance(response, dict) else None

            logger.success(
                f"Close order sent | ticker={ticker} | trigger={trigger} | body={json.dumps(order_body)} | "
                f"response={json.dumps(response)}"
            )

            notify_position_closed_async(
                ticker=ticker,
                direction=direction,
                quantity=contracts,
                entry_price=entry_price,
                exit_price=current_price,
                pnl_dollars=unrealized_pnl,
                pnl_percent=unrealized_pnl_pct,
                trigger=trigger,
                order_status=order_status,
            )
        except Exception as close_err:
            # Allow immediate retry next loop if the close request itself failed.
            PENDING_CLOSE_UNTIL.pop(close_key, None)
            response = getattr(close_err, 'response', None)
            status_code = getattr(response, 'status_code', 'unknown')
            response_body = (getattr(response, 'text', '') or '')[:1000]
            logger.error(
                f"Close order failed for {ticker}: {close_err} | status={status_code} | body={response_body or 'N/A'}"
            )

    conn.close()

def monitor_positions():
    """Run the exit monitor loop continuously."""
    logger.info(
        f"Starting position monitor loop | take_profit={TAKE_PROFIT_PERCENT:.2f}% | "
        f"stop_loss={STOP_LOSS_PERCENT:.2f}% | interval={MONITOR_INTERVAL_SECONDS}s"
    )
    while True:
        try:
            monitor_positions_once()
        except Exception as e:
            logger.error(f"Unexpected monitor loop error: {e}")
        time.sleep(MONITOR_INTERVAL_SECONDS)

if __name__ == "__main__":
    monitor_positions()