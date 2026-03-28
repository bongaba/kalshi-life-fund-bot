# --- Real-time quote access ---
# WebSocket quotes are trusted for longer — the book is maintained incrementally
# via snapshots + deltas so it should always reflect the true state.
WS_QUOTE_FRESHNESS_SECONDS = 30  # WS-maintained book stays valid much longer
REST_FALLBACK_COOLDOWN = {}      # ticker -> last REST fetch timestamp (avoid spamming)
REST_FALLBACK_MIN_INTERVAL = 10  # seconds between REST fallback calls per ticker

def get_realtime_executable_quote(ticker, direction):
    """Get the best executable bid for selling a position.

    Primary source: WebSocket-maintained orderbook (trusted for WS_QUOTE_FRESHNESS_SECONDS).
    Fallback: REST API only when WS has never provided data or is very stale.
    """
    def _extract_best_bid(orders):
        """Return the highest-priced order (best bid for a seller)."""
        if not orders:
            return None
        # Orders are sorted ascending; best bid for selling = last entry
        best = orders[-1]
        return {
            "bid": float(best[0]),
            "size": float(best[1]),
        }

    # Try WebSocket cache first (primary source)
    q = REALTIME_QUOTES.get(ticker)
    if q:
        age = (datetime.now(timezone.utc) - q["timestamp"]).total_seconds()
        if age <= WS_QUOTE_FRESHNESS_SECONDS:
            if direction == "YES":
                result = _extract_best_bid(q.get("yes_dollars", []))
            else:
                result = _extract_best_bid(q.get("no_dollars", []))
            if result:
                result["timestamp"] = q["timestamp"]
                result["source"] = "websocket"
                return result, "executable_bid"
            # WS book exists but no bids on our side — still valid data (market is thin)
            if age <= QUOTE_FRESHNESS_SECONDS:
                return None, f"no_{direction.lower()}_bids_in_ws_book"

    # Fallback to REST API — only if WS hasn't provided data or is very stale
    now_ts = time.time()
    last_rest = REST_FALLBACK_COOLDOWN.get(ticker, 0)
    if now_ts - last_rest < REST_FALLBACK_MIN_INTERVAL:
        return None, "rest_cooldown"

    try:
        logger.warning(f"WS book stale/missing for {ticker}, falling back to REST API")
        REST_FALLBACK_COOLDOWN[ticker] = now_ts
        data = signed_request("GET", f"/markets/{ticker}/orderbook")
        orderbook = data.get("orderbook_fp", data)
        yes_orders = orderbook.get("yes_dollars", [])
        no_orders = orderbook.get("no_dollars", [])
        timestamp = datetime.now(timezone.utc)

        # Seed the WS orderbook so deltas can build on it
        WS_ORDERBOOKS[ticker] = {
            "yes": {float(p): float(s) for p, s in yes_orders} if yes_orders else {},
            "no": {float(p): float(s) for p, s in no_orders} if no_orders else {},
        }
        REALTIME_QUOTES[ticker] = {
            "yes_dollars": yes_orders,
            "no_dollars": no_orders,
            "timestamp": timestamp
        }

        if direction == "YES":
            result = _extract_best_bid(yes_orders)
        else:
            result = _extract_best_bid(no_orders)
        if result:
            result["timestamp"] = timestamp
            result["source"] = "rest_fallback"
            return result, "executable_bid"
        return None, f"no_{direction.lower()}_bids_in_orderbook"
    except Exception as e:
        logger.debug(f"Failed to fetch orderbook for {ticker}: {e}")
    return None, "api_failed"

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

# WebSocket integration
from kalshi_ws_client import KalshiWebSocketClient
import os
import asyncio

setup_log_file("monitor.log")

TAKE_PROFIT_PERCENT = POSITION_TAKE_PROFIT_PERCENT
STOP_LOSS_PERCENT = POSITION_STOP_LOSS_PERCENT
MONITOR_INTERVAL_SECONDS = POSITION_MONITOR_INTERVAL_SECONDS
POSITION_CLOSE_COOLDOWN_SECONDS = max(10, MONITOR_INTERVAL_SECONDS * 2)
QUOTE_FRESHNESS_SECONDS = QUOTE_FRESHNESS_SECONDS  # loaded from config.py

# Real-time quote cache (ticker -> latest orderbook dict)
REALTIME_QUOTES = {}

# Trailing high water mark for take-profit (both YES and NO positions)
# Since current_price is always the bid for the position's side,
# profit = current_price - entry_price for both directions.
TRAILING_HIGH = {}   # ticker -> highest bid seen (peak profit point)
_TRAILING_MARKS_LOADED = False  # flag so we only load from DB once per process

# Stop-loss confirmation: require N consecutive breaches before executing
STOP_LOSS_CONFIRMATIONS_REQUIRED = 3
STOP_LOSS_CONSECUTIVE_HITS = {}  # ticker -> consecutive breach count


def _ensure_trailing_marks_table(conn):
    """Create the trailing_marks table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trailing_marks (
            ticker TEXT PRIMARY KEY,
            direction TEXT NOT NULL,
            mark_value REAL NOT NULL
        )
    """)
    conn.commit()


def _load_trailing_marks_from_db(conn):
    """Load persisted trailing marks into in-memory dicts (once per process)."""
    global _TRAILING_MARKS_LOADED
    if _TRAILING_MARKS_LOADED:
        return
    _ensure_trailing_marks_table(conn)
    rows = conn.execute("SELECT ticker, direction, mark_value FROM trailing_marks").fetchall()
    for ticker, direction, mark_value in rows:
        TRAILING_HIGH[ticker] = mark_value
    if rows:
        logger.info(f"Loaded {len(rows)} trailing mark(s) from DB")
    _TRAILING_MARKS_LOADED = True


def _save_trailing_mark(conn, ticker, direction, mark_value):
    """Upsert a trailing mark to the DB."""
    conn.execute(
        "INSERT INTO trailing_marks (ticker, direction, mark_value) VALUES (?, ?, ?) "
        "ON CONFLICT(ticker) DO UPDATE SET mark_value = excluded.mark_value",
        (ticker, direction, mark_value),
    )
    conn.commit()


def _delete_trailing_mark(conn, ticker):
    """Remove a trailing mark from the DB."""
    conn.execute("DELETE FROM trailing_marks WHERE ticker = ?", (ticker,))
    conn.commit()


def compute_smart_exit(entry_price, current_price, direction, seconds_to_close, market_duration_seconds=None, fee_per_contract=0.0, contracts=1):
    """Compute whether to exit based on binary contract economics.

    Binary contracts settle at $1.00 (win) or $0.00 (lose).
    Returns (should_exit, trigger, reason) tuple.

    All thresholds are dollar-based, proportional to max_payout:
      max_payout = contracts * (1.0 - entry_price) - total_fees
    This ensures risk/reward is always balanced regardless of entry price.

    Strategy:
      1. Stop-loss: exit if unrealized_pnl <= -(max_payout * SL_PCT)
      2. Trailing take-profit: ratcheting floor (handled separately)
    """
    # --- Max payout: total possible profit if contract settles at $1.00 ---
    total_fees = fee_per_contract * contracts
    max_payout = max(0.01, contracts * (1.0 - entry_price) - total_fees)

    # --- Unrealized P&L in dollars ---
    # current_price is already the bid for the position's side (YES bid or NO bid),
    # so profit = current - entry for both directions.
    unrealized_pnl = contracts * (current_price - entry_price) - total_fees

    # --- Stop-loss: cap loss as % of max_payout ---
    SL_PCT = 0.15  # lose at most 15% of what you could have won
    MIN_STOP_DISTANCE = 0.02  # minimum $0.02/contract price drop before stop
    sl_dollar = max_payout * SL_PCT
    # Enforce minimum stop distance so tiny max-profit positions aren't stopped by noise
    sl_dollar = max(sl_dollar, contracts * MIN_STOP_DISTANCE)

    if unrealized_pnl <= -sl_dollar:
        return True, "stop_loss", (
            f"pnl ${unrealized_pnl:.2f} breached stop -${sl_dollar:.2f} "
            f"(max_payout=${max_payout:.2f}, sl={SL_PCT:.0%})"
        )

    return False, None, None


def update_trailing_mark(ticker, current_price, direction="YES", conn=None):
    """Update the high-water mark for trailing take-profit.

    Both YES and NO positions track the highest bid seen, since current_price
    is already in the correct terms for each side.
    """
    prev = TRAILING_HIGH.get(ticker, 0.0)
    if current_price > prev:
        TRAILING_HIGH[ticker] = current_price
        if conn:
            _save_trailing_mark(conn, ticker, direction, current_price)
    return TRAILING_HIGH.get(ticker, current_price)


def check_trailing_take_profit(ticker, entry_price, current_price, direction="YES", fee_per_contract=0.0, contracts=1):
    """Check if price has dropped from peak enough to trigger trailing take-profit.

    Uses a dollar-based trailing drop: 5% of total max_payout.
    The trailing drop is converted to a per-contract price distance so it can
    be compared against the price watermarks.

    Once a position is in profit, the trailing floor is clamped to at minimum
    the breakeven price (entry + fees).  This guarantees we never exit into a
    net loss after fees are applied.
    """
    # Trailing drop: 15% of total max_payout, converted to per-contract price distance
    TRAILING_DROP_PCT = 0.15
    total_fees = fee_per_contract * contracts
    max_payout = max(0.01, contracts * (1.0 - entry_price) - total_fees)
    trailing_drop_dollars = max_payout * TRAILING_DROP_PCT
    # Convert total dollar drop to per-contract price distance
    trailing_drop = trailing_drop_dollars / contracts if contracts > 0 else 0.02
    # Minimum trailing distance to avoid noise exits
    trailing_drop = max(trailing_drop, 0.02)

    # Breakeven = entry price + round-trip fees per contract + safety buffer
    FEE_SAFETY_BUFFER = 0.01
    breakeven = entry_price + fee_per_contract + FEE_SAFETY_BUFFER

    # Both YES and NO use the same logic: track peak, exit if drops from peak
    high = TRAILING_HIGH.get(ticker, current_price)
    if high <= breakeven:
        return False, None  # never been above breakeven
    # Floor = peak minus trailing drop, but NEVER below breakeven
    floor = max(breakeven, high - trailing_drop)
    if current_price <= floor:
        return True, f"trailing TP: peak=${high:.3f} floor=${floor:.3f} breakeven=${breakeven:.3f} now=${current_price:.3f} (trail=${trailing_drop:.4f})"
    return False, None

# --- WebSocket orderbook state ---
# Full orderbook per ticker: { ticker: { "yes": {price: size, ...}, "no": {price: size, ...} } }
WS_ORDERBOOKS = {}

def _book_to_sorted_list(book_dict):
    """Convert {price: size} dict → [[price, size], ...] sorted ascending by price."""
    return sorted([[p, s] for p, s in book_dict.items() if s > 0], key=lambda x: x[0])


def handle_orderbook_ws(ticker, msg_type, data):
    """Handle orderbook_snapshot and orderbook_delta from Kalshi WS.

    Snapshots replace the entire book.  Deltas merge into the existing book
    (size=0 removes a price level).
    """
    yes_levels = data.get("yes", [])
    no_levels = data.get("no", [])

    if msg_type == "orderbook_snapshot":
        # Full replacement
        yes_book = {float(p): float(s) for p, s in yes_levels} if yes_levels else {}
        no_book = {float(p): float(s) for p, s in no_levels} if no_levels else {}
        WS_ORDERBOOKS[ticker] = {"yes": yes_book, "no": no_book}
    else:
        # Delta: merge into existing book
        existing = WS_ORDERBOOKS.get(ticker, {"yes": {}, "no": {}})
        for p, s in (yes_levels or []):
            price, size = float(p), float(s)
            if size <= 0:
                existing["yes"].pop(price, None)
            else:
                existing["yes"][price] = size
        for p, s in (no_levels or []):
            price, size = float(p), float(s)
            if size <= 0:
                existing["no"].pop(price, None)
            else:
                existing["no"][price] = size
        WS_ORDERBOOKS[ticker] = existing

    # Build sorted arrays and push to REALTIME_QUOTES
    book = WS_ORDERBOOKS[ticker]
    yes_sorted = _book_to_sorted_list(book["yes"])
    no_sorted = _book_to_sorted_list(book["no"])

    REALTIME_QUOTES[ticker] = {
        "yes_dollars": yes_sorted,
        "no_dollars": no_sorted,
        "timestamp": datetime.now(timezone.utc)
    }

    # --- Live order flow logging ---
    yes_best = yes_sorted[-1] if yes_sorted else None
    no_best = no_sorted[-1] if no_sorted else None
    delta_tag = "SNAP" if msg_type == "orderbook_snapshot" else "DELTA"
    parts = [f"[{delta_tag}] {ticker}"]
    if yes_best:
        parts.append(f"YES best_bid=${yes_best[0]:.2f}×{yes_best[1]:.0f}")
    if no_best:
        parts.append(f"NO best_bid=${no_best[0]:.2f}×{no_best[1]:.0f}")
    parts.append(f"depth=Y{len(yes_sorted)}/N{len(no_sorted)}")
    if yes_levels or no_levels:
        changes = []
        for p, s in (yes_levels or []):
            changes.append(f"Y${float(p):.2f}→{float(s):.0f}")
        for p, s in (no_levels or []):
            changes.append(f"N${float(p):.2f}→{float(s):.0f}")
        parts.append(f"changes=[{', '.join(changes)}]")
    logger.info(" | ".join(parts))

# Start the WebSocket client in a background thread and allow dynamic subscription
WS_CLIENT = None
def start_ws_client(tickers):
    global WS_CLIENT
    api_key_id = os.getenv("KALSHI_API_KEY_ID")
    private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key_id or not private_key_path:
        logger.error("KALSHI_API_KEY_ID and/or KALSHI_PRIVATE_KEY_PATH are missing from .env! WebSocket will not work.")
    # Add a known active ticker for diagnostics (replace with a real active ticker if needed)
    diagnostic_ticker = None  # "PI_XBTUSD"  # Example: Bitcoin market, adjust as needed
    all_tickers = set(tickers)
    if diagnostic_ticker:
        all_tickers.add(diagnostic_ticker)
    logger.info(f"Subscribing to tickers at startup: {sorted(all_tickers)}")
    WS_CLIENT = KalshiWebSocketClient(api_key_id, private_key_path, all_tickers, handle_orderbook_ws)
    def run():
        asyncio.run(WS_CLIENT.connect())
    t = threading.Thread(target=run, daemon=True)
    t.start()
    logger.info("Started Kalshi WebSocket client thread for real-time quotes.")
SETTLEMENT_HOLD_ENABLED = POSITION_MONITOR_HOLD_FOR_SETTLEMENT
SETTLEMENT_HOLD_SECONDS = POSITION_MONITOR_SETTLEMENT_HOLD_SECONDS
PENDING_CLOSE_UNTIL = {}
RECONCILED_SETTLED_TICKERS = set()  # Track tickers already marked settled to avoid log spam

host = "https://demo-api.kalshi.co" if MODE == "demo" else "https://api.elections.kalshi.com"
api_prefix = "/trade-api/v2"

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

    full_path = api_prefix + path
    if params:
        full_path += "?" + "&".join(f"{k}={v}" for k, v in params.items())

    sign_path = (api_prefix + path).split('?')[0]
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


def get_current_positions():
    """Get current open positions from Kalshi"""
    try:
        logger.info("Fetching positions from API...")
        data = signed_request("GET", "/portfolio/positions")
        all_positions = data.get('market_positions', [])
        # Filter to only positions with a non-zero holding (position_fp != "0.00")
        positions = [p for p in all_positions if float(p.get('position_fp', 0) or 0) != 0.0]
        logger.info(f"API returned {len(all_positions)} market positions, {len(positions)} with non-zero holdings")
        if positions:
            logger.info(f"Non-zero positions: {[p.get('ticker') for p in positions]}")
        if not positions and all_positions:
            zero_positions = [p.get('ticker', 'unknown') for p in all_positions if float(p.get('position_fp', 0) or 0) == 0.0]
            logger.info(f"Zero holding positions: {zero_positions}")
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


def _parse_timestamp(val):
    """Parse a Kalshi timestamp (ISO string, epoch int/float, or digit string) to epoch seconds."""
    if val is None:
        return None
    try:
        if isinstance(val, str):
            val = val.strip()
            if not val:
                return None
            if val.isdigit():
                ts = float(val)
            else:
                parsed = datetime.fromisoformat(val.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                ts = parsed.timestamp()
        elif isinstance(val, (int, float)):
            ts = float(val)
        else:
            return None
        if ts > 100000000000:
            ts /= 1000.0
        return ts
    except (TypeError, ValueError, OverflowError):
        return None


def _compute_market_duration(open_time, close_time):
    """Return total market duration in seconds, or None if unknown."""
    ot = _parse_timestamp(open_time)
    ct = _parse_timestamp(close_time)
    if ot and ct and ct > ot:
        return ct - ot
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
    """Return direction, volume-weighted average entry price, entry fees, and rows for ALL OPEN fills for ticker."""
    cursor.execute(
        """
        SELECT direction, price, size, COALESCE(fees, 0.0)
        FROM trades
        WHERE market_ticker = ? AND status = 'OPEN'
        ORDER BY timestamp ASC
        """,
        (ticker,),
    )
    rows = cursor.fetchall()
    if not rows:
        return None, None, 0.0, []

    direction = rows[0][0]  # all fills for a ticker should share the same direction
    total_cost = 0.0
    total_size = 0.0
    total_fees = 0.0
    for row in rows:
        price = float(row[1])
        size = float(row[2])
        fees = float(row[3])
        total_cost += price * size
        total_size += size
        total_fees += fees
    avg_entry = total_cost / total_size if total_size > 0 else 0.0
    return direction, avg_entry, total_fees, rows


def derive_entry_from_position(position, api_direction):
    """Fallback entry estimate from Kalshi position payload when local DB has no row.

    Uses total_traded_dollars / abs(position_fp) as a rough average contract cost.
    WARNING: total_traded_dollars is cumulative volume (buys + sells), so this
    over-estimates the entry price if any partial sells occurred. We clamp the
    result to the valid contract-price range [0.01, 0.99] to avoid nonsensical
    values, and mark the source as 'api_position_cost_estimate' so callers know
    this is an approximation.
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

    # Clamp to valid contract-price range
    if entry_price > 0.99:
        logger.warning(
            f"Derived entry price {entry_price:.4f} exceeds $0.99 — "
            f"total_traded_dollars ({total_cost}) likely includes sell volume. "
            f"Clamping to $0.99."
        )
        entry_price = 0.99
    elif entry_price < 0.01:
        entry_price = 0.01

    return api_direction, entry_price, "api_position_cost_estimate"


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


def reconcile_db_vs_exchange(cursor, ticker, api_contracts, open_fills):
    """Close stale OPEN rows in FIFO order when DB has more contracts than the exchange.

    Args:
        cursor: DB cursor
        ticker: market ticker
        api_contracts: actual contract count on the exchange (from position_fp)
        open_fills: list of (direction, price, size) rows from get_db_entry_for_ticker,
                    ordered by timestamp ASC
    Returns:
        Number of rows closed.
    """
    # Count DB contracts: each fill's size * 100 = contract count
    db_contract_count = 0
    for row in open_fills:
        size = float(row[2])
        db_contract_count += int(round(size * 100))

    excess = db_contract_count - api_contracts
    if excess <= 0:
        return 0

    logger.warning(
        f"Reconciliation: {ticker} has {db_contract_count} OPEN contracts in DB "
        f"but only {api_contracts} on exchange. Closing {excess} excess contracts (FIFO)."
    )

    # Close oldest fills first (rows are ASC by timestamp)
    closed_count = 0
    contracts_to_close = excess
    for row in open_fills:
        if contracts_to_close <= 0:
            break
        fill_price = float(row[1])
        fill_size = float(row[2])
        fill_contracts = int(round(fill_size * 100))

        if fill_contracts <= contracts_to_close:
            # Close entire fill
            cursor.execute(
                """
                UPDATE trades SET status = 'CLOSED', reason = reason || ' | auto-reconciled',
                    resolved_timestamp = datetime('now')
                WHERE market_ticker = ? AND status = 'OPEN' AND price = ? AND size = ?
                    AND id = (
                        SELECT id FROM trades
                        WHERE market_ticker = ? AND status = 'OPEN' AND price = ? AND size = ?
                        ORDER BY timestamp ASC, id ASC LIMIT 1
                    )
                """,
                (ticker, fill_price, fill_size, ticker, fill_price, fill_size),
            )
            contracts_to_close -= fill_contracts
            closed_count += 1
        else:
            # Partial close: reduce the fill size, close the excess portion
            remaining_size = (fill_contracts - contracts_to_close) / 100.0
            cursor.execute(
                """
                UPDATE trades SET size = ?
                WHERE market_ticker = ? AND status = 'OPEN' AND price = ? AND size = ?
                    AND id = (
                        SELECT id FROM trades
                        WHERE market_ticker = ? AND status = 'OPEN' AND price = ? AND size = ?
                        ORDER BY timestamp ASC, id ASC LIMIT 1
                    )
                """,
                (remaining_size, ticker, fill_price, fill_size, ticker, fill_price, fill_size),
            )
            contracts_to_close = 0
            closed_count += 1

    return closed_count


def mark_fills_closed(cursor, ticker, exit_price, trigger):
    """Mark all OPEN fills for a ticker as CLOSED after a successful close order."""
    cursor.execute(
        """
        UPDATE trades
        SET status = 'CLOSED',
            pnl = (? - price) * size,
            reason = reason || ' | closed by ' || ?,
            resolved_timestamp = datetime('now')
        WHERE market_ticker = ? AND status = 'OPEN'
        """,
        (exit_price, trigger, ticker),
    )
    return cursor.rowcount


def place_ioc_close_order(ticker, direction, count, executable_quote, slippage_pct=0.0):
    """Send sell-to-close IOC order for YES/NO side.
    
    slippage_pct: percentage below the bid to price the order (e.g. 0.02 = 2%).
    A higher value increases fill probability at the cost of worse price.
    On Kalshi, you still get price improvement if a higher bid exists.
    """
    side = "yes" if direction == "YES" else "no"
    client_order_id = f"position-close-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

    bid_cents = int(round(executable_quote["bid"] * 100))
    slippage_cents = int(round(bid_cents * slippage_pct))
    aggressive_price = max(1, bid_cents - slippage_cents)  # floor at $0.01

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
        order_body["yes_price"] = aggressive_price
    else:
        order_body["no_price"] = aggressive_price

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
    entry_fees=0.0,
    exit_fees=0.0,
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
            "entry_fees": entry_fees,
            "exit_fees": exit_fees,
        },
        daemon=True,
    )
    thread.start()


def monitor_positions_once():
    """Monitor live positions and submit IOC close orders at configured P&L % thresholds."""
    logger.info("Checking open positions for take-profit/stop-loss exits...")

    positions = get_current_positions()

    conn = get_db_connection()
    cursor = conn.cursor()

    # Load persisted trailing marks on first run
    _load_trailing_marks_from_db(conn)

    # --- Reconcile: close DB fills for tickers with zero exchange position ---
    api_tickers = {p.get('ticker') for p in positions if p.get('ticker')} if positions else set()
    cursor.execute("SELECT DISTINCT market_ticker FROM trades WHERE status = 'OPEN'")
    db_open_tickers = {row[0] for row in cursor.fetchall()}
    stale_tickers = db_open_tickers - api_tickers - RECONCILED_SETTLED_TICKERS
    for stale_ticker in stale_tickers:
        rows_closed = cursor.execute(
            """
            UPDATE trades
            SET status = 'SETTLED',
                reason = reason || ' | auto-settled (zero exchange position)',
                resolved_timestamp = datetime('now')
            WHERE market_ticker = ? AND status = 'OPEN'
            """,
            (stale_ticker,),
        ).rowcount
        if rows_closed:
            logger.warning(
                f"Reconciliation: {stale_ticker} has 0 contracts on exchange but {rows_closed} "
                f"OPEN fill(s) in DB — marked as SETTLED."
            )
        RECONCILED_SETTLED_TICKERS.add(stale_ticker)
    if stale_tickers:
        conn.commit()

    if not positions:
        logger.warning("No open positions found or API failed. If this is unexpected, check API/WebSocket health.")
        conn.close()
        return

    logger.info(f"Processing {len(positions)} positions from API...")
    
    for position in positions:
        ticker = position.get('ticker')
        # Dynamically subscribe to new tickers if needed
        if ticker and WS_CLIENT and ticker not in WS_CLIENT.tickers:
            try:
                ws_loop = getattr(WS_CLIENT, '_loop', None)
                if ws_loop and ws_loop.is_running():
                    asyncio.run_coroutine_threadsafe(WS_CLIENT.subscribe_ticker(ticker), ws_loop)
                    logger.info(f"Requested dynamic WS subscription for {ticker}")
                    # Brief wait for first snapshot (non-blocking feel: 2 x 250ms)
                    for _ in range(2):
                        if ticker in REALTIME_QUOTES:
                            break
                        time.sleep(0.25)
                else:
                    logger.debug(f"WS loop not available for dynamic subscription of {ticker}")
            except Exception as e:
                logger.debug(f"Failed to dynamically subscribe to {ticker}: {e}")

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
        db_direction, entry_price, entry_fees, open_fills = get_db_entry_for_ticker(cursor, ticker)
        entry_source = "trades_db"

        # Reconcile: if DB has more OPEN contracts than exchange, close excess FIFO
        if open_fills:
            closed = reconcile_db_vs_exchange(cursor, ticker, contracts, open_fills)
            if closed > 0:
                conn.commit()
                # Re-fetch after reconciliation
                db_direction, entry_price, entry_fees, open_fills = get_db_entry_for_ticker(cursor, ticker)

        if entry_price is None:
            fallback_direction, fallback_entry_price, fallback_source = derive_entry_from_position(position, api_direction)
            if fallback_entry_price is None:
                logger.info(f"Skipping {ticker}: no OPEN entry row found in trades.db and no usable API cost basis (API position_fp={raw_position})")
                continue
            db_direction = fallback_direction
            entry_price = fallback_entry_price
            entry_fees = 0.0
            entry_source = fallback_source
            reconcile_open_position(cursor, ticker, db_direction, contracts, entry_price, entry_source)
            conn.commit()
            logger.warning(
                f"{ticker}: no OPEN entry row found in trades.db; reconciled live position into DB "
                f"with entry={entry_price:.4f} source={entry_source}"
            )

        direction = db_direction if db_direction in {"YES", "NO"} else api_direction
        market = get_market(ticker)
        # Use real-time WebSocket quote
        executable_quote, quote_reason = get_realtime_executable_quote(ticker, direction)
        hold_for_settlement, hold_reason = QUOTE_ENGINE.should_hold_for_settlement(ticker, market)

        # Graceful degradation: never act on stale/missing data
        if executable_quote is None:
            if hold_for_settlement:
                logger.info(f"Holding {ticker} for settlement: {hold_reason}")
            else:
                if quote_reason.startswith("stale"):
                    logger.error(f"ALERT: Skipping {ticker} {direction} due to stale quote ({quote_reason}). WebSocket may be lagging or disconnected.")
                else:
                    logger.warning(f"Skipping {ticker} {direction}: no real-time quote ({quote_reason}). Market may be thin.")
            continue

        if hold_for_settlement:
            logger.info(
                f"Holding {ticker}: settlement-aware gate active ({hold_reason}) "
                f"with fresh_quote_source=websocket"
            )
            continue


        current_price = executable_quote["bid"]

        # Compute per-contract fee (entry + estimated exit)
        # Estimate exit fee ≈ entry fee per contract (same Kalshi rate applies both sides)
        entry_fee_per_contract = (entry_fees / contracts) if contracts > 0 else 0.0
        est_exit_fee_per_contract = entry_fee_per_contract  # same rate
        fee_per_contract = entry_fee_per_contract + est_exit_fee_per_contract

        unrealized_pnl = contracts * (current_price - entry_price) - entry_fees - (est_exit_fee_per_contract * contracts)
        unrealized_pnl_pct = ((current_price - entry_price - fee_per_contract) / entry_price * 100.0) if entry_price > 0 else 0.0

        # Compute time to close for smart exit
        seconds_to_close = calculate_seconds_to_close(market.get("close_time"))

        # Compute total market duration for proportional hold gate
        market_duration_seconds = _compute_market_duration(market.get("open_time"), market.get("close_time"))

        # Update trailing high/low water mark
        update_trailing_mark(ticker, current_price, direction, conn=conn)

        # Smart exit decision
        should_exit, trigger, exit_reason = compute_smart_exit(
            entry_price, current_price, direction, seconds_to_close, market_duration_seconds,
            fee_per_contract=fee_per_contract, contracts=contracts
        )

        # Stop-loss confirmation: require N consecutive breaches to filter volatility
        # BUT: skip confirmations for severe breaches (2x+ threshold) — exit immediately
        if should_exit and trigger == "stop_loss":
            # Parse the unrealized PnL and SL threshold from the exit reason to check severity
            total_fees = fee_per_contract * contracts
            max_payout = max(0.01, contracts * (1.0 - entry_price) - total_fees)
            unrealized_pnl = contracts * (current_price - entry_price) - total_fees
            sl_dollar = max(max_payout * 0.15, contracts * 0.02)
            severe_breach = unrealized_pnl <= -(sl_dollar * 2)  # loss is 2x+ the threshold

            if severe_breach:
                logger.warning(
                    f"{ticker}: SEVERE stop-loss breach (pnl=${unrealized_pnl:.2f} vs threshold -${sl_dollar:.2f}) — "
                    f"skipping confirmation, exiting immediately"
                )
                exit_reason = f"{exit_reason} (severe breach — immediate exit)"
            else:
                hits = STOP_LOSS_CONSECUTIVE_HITS.get(ticker, 0) + 1
                STOP_LOSS_CONSECUTIVE_HITS[ticker] = hits
                if hits < STOP_LOSS_CONFIRMATIONS_REQUIRED:
                    logger.warning(
                        f"{ticker}: stop-loss breach {hits}/{STOP_LOSS_CONFIRMATIONS_REQUIRED} | "
                        f"{exit_reason} — waiting for confirmation"
                    )
                    should_exit = False
                    trigger = None
                    exit_reason = None
                else:
                    exit_reason = f"{exit_reason} (confirmed {hits}/{STOP_LOSS_CONFIRMATIONS_REQUIRED})"
        elif trigger != "stop_loss":
            # Price recovered above stop — reset counter
            STOP_LOSS_CONSECUTIVE_HITS.pop(ticker, None)

        # Check trailing take-profit if smart exit didn't trigger
        if not should_exit:
            should_exit, trailing_reason = check_trailing_take_profit(ticker, entry_price, current_price, direction, fee_per_contract=fee_per_contract, contracts=contracts)
            if should_exit:
                trigger = "take_profit"
                exit_reason = trailing_reason

        ttc_str = f"{seconds_to_close:.0f}s" if seconds_to_close is not None else "unknown"
        trail_str = f"${TRAILING_HIGH.get(ticker, 0):.3f}"

        logger.info(
            f"{ticker} | dir={direction} | contracts={contracts} | entry={entry_price:.3f} | "
            f"entry_source={entry_source} | mark={current_price:.3f} | quote_source={executable_quote['source']} | "
            f"unrealized_pnl=${unrealized_pnl:.2f} | pnl_pct={unrealized_pnl_pct:.2f}% | "
            f"ttc={ttc_str} | trail={trail_str} | exit={'YES:'+trigger if should_exit else 'NO'}"
        )

        if not should_exit:
            if exit_reason:
                logger.debug(f"{ticker}: {exit_reason}")
            continue
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
            f"reason={exit_reason}"
        )


        try:
            # Percentage-based slippage: scales with price so you never give up too much
            # Stop-losses are more aggressive to guarantee a fill
            if trigger == "stop_loss":
                slippage_schedule = [0.02, 0.04, 0.06]  # 2%, 4%, 6% below bid
            else:
                slippage_schedule = [0.01, 0.02, 0.04]  # 1%, 2%, 4% below bid

            order_filled = False
            for attempt, slippage in enumerate(slippage_schedule, 1):
                fresh_quote, fresh_reason = get_realtime_executable_quote(ticker, direction)
                if not fresh_quote:
                    logger.error(f"Atomic close failed: no fresh quote for {ticker} {direction} (reason={fresh_reason})")
                    break

                PENDING_CLOSE_UNTIL[close_key] = now_ts + POSITION_CLOSE_COOLDOWN_SECONDS
                response, order_body = place_ioc_close_order(ticker, direction, contracts, fresh_quote, slippage_pct=slippage)
                order_data = response.get("order", {}) if isinstance(response, dict) else {}
                order_status = order_data.get("status") or response.get("status") if isinstance(response, dict) else None

                if order_status in ("canceled", "cancelled"):
                    slippage_cents = int(round(fresh_quote['bid'] * 100 * slippage))
                    logger.warning(
                        f"IOC close attempt {attempt}/{len(slippage_schedule)} for {ticker} canceled "
                        f"(slippage={slippage:.0%} = {slippage_cents}¢). {'Retrying...' if attempt < len(slippage_schedule) else 'Will retry next cycle.'}"
                    )
                    time.sleep(0.3)  # Brief pause between retries
                    continue

                order_filled = True
                break

            if not order_filled:
                PENDING_CLOSE_UNTIL.pop(close_key, None)  # Allow retry next cycle
                continue

            # Fetch actual fill price and fees from exchange
            sell_side = "yes" if direction == "YES" else "no"
            kalshi_order_id = order_data.get('order_id')
            actual_exit_price, exit_fees = fetch_fill_price_and_fees(kalshi_order_id, sell_side) if kalshi_order_id else (None, 0.0)

            if actual_exit_price is not None:
                exit_price = actual_exit_price
                logger.info(f"Actual exit fill price for {ticker}: ${exit_price:.4f} (fees: ${exit_fees:.2f})")
            else:
                exit_price = fresh_quote["bid"]
                exit_fees = 0.0
                logger.warning(f"Could not fetch exit fills for {ticker}, using quote price ${exit_price:.4f}")

            realized_pnl = contracts * (exit_price - entry_price) - entry_fees - exit_fees
            realized_pnl_pct = ((exit_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0

            logger.success(
                f"Close order sent | ticker={ticker} | trigger={trigger} | body={json.dumps(order_body)} | "
                f"response={json.dumps(response)}"
            )

            # Mark all OPEN fills in DB as CLOSED with realized P&L
            rows_closed = mark_fills_closed(cursor, ticker, exit_price, trigger)
            conn.commit()
            logger.info(f"Marked {rows_closed} OPEN fill(s) as CLOSED for {ticker} at exit={exit_price:.4f}")

            # Clean trailing marks for closed position
            TRAILING_HIGH.pop(ticker, None)
            STOP_LOSS_CONSECUTIVE_HITS.pop(ticker, None)
            _delete_trailing_mark(conn, ticker)

            # Alert if loss exceeds -20% (severe loss on binary contract)
            if trigger == "stop_loss" and realized_pnl_pct < -20.0:
                logger.error(f"ALERT: Close order for {ticker} executed with severe loss {realized_pnl_pct:.2f}%")

            notify_position_closed_async(
                ticker=ticker,
                direction=direction,
                quantity=contracts,
                entry_price=entry_price,
                exit_price=exit_price,
                pnl_dollars=realized_pnl,
                pnl_percent=realized_pnl_pct,
                trigger=trigger,
                order_status=order_status,
                entry_fees=entry_fees,
                exit_fees=exit_fees,
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
        f"Starting position monitor loop | exit_strategy=smart_binary | "
        f"interval={MONITOR_INTERVAL_SECONDS}s | "
        f"hold_for_settlement={SETTLEMENT_HOLD_ENABLED} | settlement_window={SETTLEMENT_HOLD_SECONDS}s"
    )
    while True:
        try:
            monitor_positions_once()
        except Exception as e:
            logger.error(f"Unexpected monitor loop error: {e}")
        time.sleep(MONITOR_INTERVAL_SECONDS)

if __name__ == "__main__":
    # Fetch all tickers to monitor (from open positions at startup)
    try:
        initial_positions = get_current_positions()
        tickers = [p.get('ticker') for p in initial_positions if p.get('ticker')]
        if tickers:
            start_ws_client(tickers)
        else:
            logger.warning("No tickers found to subscribe to WebSocket at startup.")
    except Exception as e:
        logger.error(f"Failed to fetch initial tickers for WebSocket: {e}")
    monitor_positions()