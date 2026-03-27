import time
import sqlite3
import requests
import base64
import json
import uuid
import threading
from datetime import datetime, timezone
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
QUOTE_FRESHNESS_SECONDS = 5
SETTLEMENT_HOLD_ENABLED = POSITION_MONITOR_HOLD_FOR_SETTLEMENT
SETTLEMENT_HOLD_SECONDS = POSITION_MONITOR_SETTLEMENT_HOLD_SECONDS
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
        all_positions = data.get('market_positions', [])
        # Filter to only positions with a non-zero holding (position_fp != "0.00")
        positions = [p for p in all_positions if float(p.get('position_fp', 0) or 0) != 0.0]
        logger.info(f"API returned {len(all_positions)} market positions, {len(positions)} with non-zero holdings")
        return positions
    except Exception as e:
        logger.error(f"Failed to get positions: {e}")
        logger.error(f"Response details: {getattr(e, 'response', None)}")
        return []


def parse_market_price(value):
    if value in (None, ""):
        return 0.0
    try:
        price = float(value)
    except (TypeError, ValueError):
        return 0.0
    if price > 1.0:
        price /= 100.0
    return max(0.0, min(1.0, price))


def parse_size_fp(value):
    if value in (None, ""):
        return 0.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def price_to_cents(price):
    return min(99, max(1, int(round(price * 100))))


def prices_are_complementary(left_price, right_price, tolerance=0.011):
    return abs((left_price + right_price) - 1.0) <= tolerance


def calculate_seconds_to_close(close_time):
    if close_time in (None, ""):
        return None

    close_timestamp = None

    try:
        if isinstance(close_time, str):
            close_time = close_time.strip()
            if not close_time:
                return None

            if close_time.isdigit():
                close_timestamp = float(close_time)
            else:
                parsed_datetime = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                if parsed_datetime.tzinfo is None:
                    parsed_datetime = parsed_datetime.replace(tzinfo=timezone.utc)
                close_timestamp = parsed_datetime.timestamp()
        elif isinstance(close_time, (int, float)):
            close_timestamp = float(close_time)
        else:
            return None

        if close_timestamp > 100000000000:
            close_timestamp /= 1000.0

        current_timestamp = datetime.now(timezone.utc).timestamp()
        return max(0.0, close_timestamp - current_timestamp)
    except (TypeError, ValueError, OverflowError):
        return None


class QuoteEngine:
    def __init__(self, freshness_seconds=QUOTE_FRESHNESS_SECONDS):
        self.last_valid = {}
        self.freshness_seconds = freshness_seconds

    def _build_snapshot(self, market, source):
        yes_bid = parse_market_price(market.get("yes_bid_dollars", market.get("yes_bid", 0)))
        no_bid = parse_market_price(market.get("no_bid_dollars", market.get("no_bid", 0)))
        yes_ask = parse_market_price(market.get("yes_ask_dollars", market.get("yes_ask", 0)))
        no_ask = parse_market_price(market.get("no_ask_dollars", market.get("no_ask", 0)))
        yes_bid_size = parse_size_fp(market.get("yes_bid_size_fp", market.get("yes_bid_size", 0)))
        no_bid_size = parse_size_fp(market.get("no_bid_size_fp", market.get("no_bid_size", 0)))
        yes_ask_size = parse_size_fp(market.get("yes_ask_size_fp", market.get("yes_ask_size", 0)))
        no_ask_size = parse_size_fp(market.get("no_ask_size_fp", market.get("no_ask_size", 0)))
        settlement_timer_seconds = market.get("settlement_timer_seconds")

        # Kalshi REST sometimes returns a valid binary price for one side but omits the
        # matching size field while still exposing the complementary ask size on the other side.
        # Example seen live: no_bid_dollars=0.96, no_bid_size_fp=null, yes_ask_dollars=0.04,
        # yes_ask_size_fp=1545. In that case the YES ask size is the best available executable
        # liquidity for selling NO at the complementary price.
        if no_bid > 0 and no_bid_size <= 0 and yes_ask > 0 and yes_ask_size > 0 and prices_are_complementary(no_bid, yes_ask):
            no_bid_size = yes_ask_size

        if yes_bid > 0 and yes_bid_size <= 0 and no_ask > 0 and no_ask_size > 0 and prices_are_complementary(yes_bid, no_ask):
            yes_bid_size = no_ask_size

        try:
            settlement_timer_seconds = int(float(settlement_timer_seconds))
        except (TypeError, ValueError):
            settlement_timer_seconds = None

        return {
            "yes_bid_dollars": yes_bid,
            "no_bid_dollars": no_bid,
            "yes_ask_dollars": yes_ask,
            "no_ask_dollars": no_ask,
            "yes_bid_size_fp": yes_bid_size,
            "no_bid_size_fp": no_bid_size,
            "yes_ask_size_fp": yes_ask_size,
            "no_ask_size_fp": no_ask_size,
            "yes_bid_cents": price_to_cents(yes_bid) if yes_bid > 0 else 0,
            "no_bid_cents": price_to_cents(no_bid) if no_bid > 0 else 0,
            "timestamp": datetime.now(timezone.utc),
            "source": source,
            "close_time": market.get("close_time"),
            "settlement_timer_seconds": settlement_timer_seconds,
            "market_status": str(market.get("status") or "").lower(),
        }

    def is_valid_quote(self, snapshot):
        return (
            snapshot["yes_bid_dollars"] > 0 and snapshot["yes_bid_size_fp"] > 0
        ) or (
            snapshot["no_bid_dollars"] > 0 and snapshot["no_bid_size_fp"] > 0
        )

    def update(self, ticker, market, source="rest"):
        snapshot = self._build_snapshot(market, source)
        if self.is_valid_quote(snapshot):
            self.last_valid[ticker] = snapshot
            return snapshot, True
        return snapshot, False

    def get_executable_quote(self, ticker, direction):
        snapshot = self.last_valid.get(ticker)
        if not snapshot:
            return None, "no_data", None

        age_seconds = (datetime.now(timezone.utc) - snapshot["timestamp"]).total_seconds()
        if age_seconds > self.freshness_seconds:
            return None, f"stale:{age_seconds:.1f}s", snapshot

        if direction == "YES":
            if snapshot["yes_bid_dollars"] > 0 and snapshot["yes_bid_size_fp"] > 0:
                return snapshot, "executable_bid", snapshot
        else:
            if snapshot["no_bid_dollars"] > 0 and snapshot["no_bid_size_fp"] > 0:
                return snapshot, "executable_bid", snapshot

        return None, "no_valid_quote", snapshot

    def should_hold_for_settlement(self, ticker, current_market=None):
        if not SETTLEMENT_HOLD_ENABLED:
            return False, None

        cached = self.last_valid.get(ticker, {})
        market_status = str((current_market or {}).get("status") or cached.get("market_status") or "").lower()
        if market_status in {"finalized", "settled", "resolved", "closed"}:
            return True, f"market_status={market_status}"

        settlement_timer_seconds = (current_market or {}).get("settlement_timer_seconds")
        if settlement_timer_seconds in (None, ""):
            settlement_timer_seconds = cached.get("settlement_timer_seconds")

        try:
            settlement_timer_seconds = int(float(settlement_timer_seconds))
        except (TypeError, ValueError):
            settlement_timer_seconds = None

        if settlement_timer_seconds is not None and settlement_timer_seconds <= SETTLEMENT_HOLD_SECONDS:
            return True, f"settlement_timer_seconds={settlement_timer_seconds}"

        close_time = (current_market or {}).get("close_time") or cached.get("close_time")
        seconds_to_close = calculate_seconds_to_close(close_time)
        if seconds_to_close is not None and seconds_to_close <= SETTLEMENT_HOLD_SECONDS:
            return True, f"close_time_in={seconds_to_close:.0f}s"

        return False, None


QUOTE_ENGINE = QuoteEngine()

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


def get_market(ticker):
    """Return raw market payload for a ticker."""
    try:
        data = signed_request("GET", f"/markets/{ticker}")
        return data.get('market', {})
    except Exception as e:
        logger.error(f"Failed to get market for {ticker}: {e}")
        return {}


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


def derive_entry_from_position(position, api_direction):
    """Fallback entry estimate from Kalshi position payload when local DB has no row.

    Uses total_traded_dollars / abs(position_fp) as an average contract cost for the
    currently held side. This is less authoritative than the local execution log, but
    it is far better than skipping risk management entirely for positions missing from
    trades.db (for example manual trades or previously untracked fills).
    """
    raw_position = position.get("position_fp", 0)
    total_traded_dollars = position.get("total_traded_dollars", 0)

    try:
        contracts = abs(float(raw_position))
        total_cost = float(total_traded_dollars)
    except (TypeError, ValueError):
        return None, None, None

    if contracts <= 0 or total_cost <= 0:
        return None, None, None

    entry_price = total_cost / contracts
    if entry_price <= 0:
        return None, None, None

    return api_direction, entry_price, "api_position_cost"


def reconcile_open_position(cursor, ticker, direction, contracts, entry_price, entry_source):
    """Persist a synthetic OPEN row for a live exchange position missing from trades.db."""
    synthetic_client_order_id = f"reconciled-{ticker}-{int(time.time() * 1000)}"
    size_units = contracts / 100.0
    cursor.execute(
        '''
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
            order_status
        )
        VALUES (datetime('now'), ?, ?, ?, ?, 0.0, ?, 'OPEN', ?, ?, ?)
        ''',
        (
            ticker,
            direction,
            size_units,
            entry_price,
            f"reconciled_from_{entry_source}",
            synthetic_client_order_id,
            None,
            "reconciled",
        )
    )


def place_ioc_close_order(ticker, direction, count, executable_quote):
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
        "post_only": False,
        "client_order_id": client_order_id,
    }

    if side == "yes":
        order_body["yes_price"] = int(executable_quote["yes_bid_cents"])
    else:
        order_body["no_price"] = int(executable_quote["no_bid_cents"])

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

    logger.info(f"Processing {len(positions)} positions from API...")
    
    for position in positions:
        ticker = position.get('ticker')
        # The API returns position_fp (fixed point format like "-78.00" for shorts)
        raw_position = position.get('position_fp', '0')

        try:
            contracts = int(abs(float(raw_position)))
        except (TypeError, ValueError) as e:
            logger.warning(f"Failed to parse contracts for {ticker}: {e}")
            contracts = 0

        if not ticker or contracts <= 0:
            continue

        api_direction = "YES" if float(raw_position) > 0 else "NO"
        db_direction, entry_price = get_db_entry_for_ticker(cursor, ticker)
        entry_source = "trades_db"
        if entry_price is None:
            fallback_direction, fallback_entry_price, fallback_source = derive_entry_from_position(position, api_direction)
            if fallback_entry_price is None:
                logger.info(f"Skipping {ticker}: no OPEN entry row found in trades.db and no usable API cost basis (API position_fp={raw_position})")
                continue
            db_direction = fallback_direction
            entry_price = fallback_entry_price
            entry_source = fallback_source
            reconcile_open_position(cursor, ticker, db_direction, contracts, entry_price, entry_source)
            conn.commit()
            logger.warning(
                f"{ticker}: no OPEN entry row found in trades.db; reconciled live position into DB "
                f"with entry={entry_price:.4f} source={entry_source}"
            )

        direction = db_direction if db_direction in {"YES", "NO"} else api_direction
        market = get_market(ticker)
        snapshot, has_live_executable_quote = QUOTE_ENGINE.update(ticker, market, source="rest")
        executable_quote, quote_reason, cached_snapshot = QUOTE_ENGINE.get_executable_quote(ticker, direction)
        hold_for_settlement, hold_reason = QUOTE_ENGINE.should_hold_for_settlement(ticker, market)

        if executable_quote is None:
            if hold_for_settlement:
                logger.info(f"Holding {ticker} for settlement: {hold_reason}")
            else:
                logger.warning(
                    f"Skipping {ticker}: no valid executable quote for {direction} "
                    f"(reason={quote_reason}, live_quote={has_live_executable_quote})"
                )
            continue

        if hold_for_settlement:
            logger.info(
                f"Holding {ticker}: settlement-aware gate active ({hold_reason}) "
                f"with fresh_quote_source={cached_snapshot.get('source') if cached_snapshot else 'none'}"
            )
            continue

        current_price = (
            executable_quote["yes_bid_dollars"] if direction == "YES" else executable_quote["no_bid_dollars"]
        )

        unrealized_pnl = contracts * (current_price - entry_price)
        unrealized_pnl_pct = ((current_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0

        logger.info(
            f"{ticker} | dir={direction} | contracts={contracts} | entry={entry_price:.3f} | "
            f"entry_source={entry_source} | mark={current_price:.3f} | quote_source={executable_quote['source']} | "
            f"unrealized_pnl=${unrealized_pnl:.2f} | pnl_pct={unrealized_pnl_pct:.2f}%"
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
            response, order_body = place_ioc_close_order(ticker, direction, contracts, executable_quote)
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
        f"stop_loss={STOP_LOSS_PERCENT:.2f}% | interval={MONITOR_INTERVAL_SECONDS}s | "
        f"hold_for_settlement={SETTLEMENT_HOLD_ENABLED} | settlement_window={SETTLEMENT_HOLD_SECONDS}s"
    )
    while True:
        try:
            monitor_positions_once()
        except Exception as e:
            logger.error(f"Unexpected monitor loop error: {e}")
        time.sleep(MONITOR_INTERVAL_SECONDS)

if __name__ == "__main__":
    monitor_positions()