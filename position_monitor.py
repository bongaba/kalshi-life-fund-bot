# --- Real-time quote access ---
# WebSocket quotes are trusted for longer — the book is maintained incrementally
# via snapshots + deltas so it should always reflect the true state.
WS_QUOTE_FRESHNESS_SECONDS = 10  # WS-maintained book — tight window to detect disconnects
REST_FALLBACK_COOLDOWN = {}      # ticker -> last REST fetch timestamp (avoid spamming)
REST_FALLBACK_MIN_INTERVAL = 3   # seconds between REST fallback calls per ticker

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

    def _infer_from_opposite(opposite_orders, timestamp, source_tag):
        """When our side has no bids, infer price from the opposite side's best bid.
        If YES best bid = $0.18 → implied NO value ≈ 1 - 0.18 = $0.82.
        Marked as 'inferred' so downstream knows this isn't a real executable quote."""
        opp = _extract_best_bid(opposite_orders)
        if opp:
            inferred_price = round(1.0 - opp["bid"], 2)
            if 0.01 <= inferred_price <= 0.99:
                return {
                    "bid": inferred_price,
                    "size": 0,  # no real liquidity on our side
                    "timestamp": timestamp,
                    "source": f"{source_tag}_inferred_from_opposite",
                }, "inferred_bid"
        return None, None

    # Try WebSocket cache first (primary source)
    with _BOOK_LOCK:
        q = REALTIME_QUOTES.get(ticker)
        if q:
            q = dict(q)  # shallow copy for thread safety
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
            # No bids on our side — try to infer from opposite side
            opposite_key = "no_dollars" if direction == "YES" else "yes_dollars"
            inferred, inferred_reason = _infer_from_opposite(
                q.get(opposite_key, []), q["timestamp"], "websocket"
            )
            if inferred:
                logger.info(
                    f"[INFERRED] {ticker} {direction}: no {direction} bids, inferred "
                    f"${inferred['bid']:.2f} from opposite side"
                )
                return inferred, inferred_reason
            # WS book exists but no bids on either side
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
        with _BOOK_LOCK:
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
        # No bids on our side via REST — try opposite side inference
        opposite_orders = no_orders if direction == "YES" else yes_orders
        inferred, inferred_reason = _infer_from_opposite(opposite_orders, timestamp, "rest")
        if inferred:
            logger.info(
                f"[INFERRED] {ticker} {direction}: no {direction} bids in REST, inferred "
                f"${inferred['bid']:.2f} from opposite side"
            )
            return inferred, inferred_reason
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
from logging_setup import setup_log_file, setup_error_log, setup_trade_decision_log, setup_stop_loss_log
from config import *
from discord_notifications import notify_position_closed
import discord_bot
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

# WebSocket integration
from kalshi_ws_client import KalshiWebSocketClient
import os
import asyncio

setup_log_file("monitor.log")
setup_error_log()
setup_trade_decision_log()
setup_stop_loss_log()

# Bound logger for structured stop-loss JSON-lines log
sl_logger = logger.bind(sl_log=True)

def log_sl_event(event, **kwargs):
    """Write a structured JSON-lines record to the stop-loss log."""
    import json as _json
    record = {
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(),
        **kwargs,
    }
    sl_logger.info(_json.dumps(record, default=str))

MONITOR_INTERVAL_SECONDS = POSITION_MONITOR_INTERVAL_SECONDS
POSITION_CLOSE_COOLDOWN_SECONDS = max(10, MONITOR_INTERVAL_SECONDS * 2)

# Real-time quote cache (ticker -> latest orderbook dict)
REALTIME_QUOTES = {}

# Lock for global state accessed by both the monitor loop and daemon exit threads
_STATE_LOCK = threading.Lock()

# Stop-loss state tracking (per-ticker)
from collections import deque
TRAILING_EMA = {}           # ticker -> EMA-smoothed bid price (momentum reversal peak tracking only)
TRAILING_PEAK = {}          # ticker -> highest smoothed position value since entry
STOP_LOSS_BREACH_START = {} # ticker -> time.monotonic() of first breach
RECENT_BID_SIZES = {}       # ticker -> deque of last 8 bid sizes
MARK_HISTORY = {}           # ticker -> deque of (monotonic_time, mark_price) for stagnation detection
EMA_UPDATE_COUNT = {}       # ticker -> int (number of EMA updates, for warmup gating)
SL_EXIT_COOLDOWN = {}       # ticker -> monotonic time of last SL exit (re-entry prevention)

# Orderbook momentum guard: opposite-side pressure detection
MOMENTUM_GUARD_DELTA_FLOOR = 300        # absolute minimum delta to count as pressure
MOMENTUM_GUARD_BEST_BID_RATIO = 0.60   # delta must be >= this fraction of best bid size
MOMENTUM_GUARD_WINDOW_SECONDS = 8.0     # how recent the signal must be
MOMENTUM_GUARD_ACCELERATED_WAIT = 1.0   # reduced sustained wait when pressure detected
OPPOSITE_PRESSURE_SIGNALS = {}          # ticker -> {"side": "yes"|"no", "delta": float, "time": float}


def compute_smart_exit(entry_price, current_price, direction, seconds_to_close,
                       market_duration_seconds=None, fee_per_contract=0.0, contracts=1,
                       bid_size=0, ticker=None):
    """Compute whether to exit based on binary contract economics.

    Binary contracts settle at $1.00 (win) or $0.00 (lose).
    Returns (should_exit, trigger, reason) tuple.

    When stop-loss is disabled via config, always returns (False, None, None).
    """
    if not POSITION_STOP_LOSS_ENABLED:
        return False, None, None

    total_fees = fee_per_contract * contracts
    unrealized_pnl = contracts * (current_price - entry_price) - total_fees
    position_value = contracts * entry_price

    # === 1. Update EMA smoothed price (noise filter, α=0.25) ===
    if ticker:
        with _STATE_LOCK:
            if ticker not in TRAILING_EMA:
                TRAILING_EMA[ticker] = current_price
            else:
                TRAILING_EMA[ticker] = (0.25 * current_price) + (0.75 * TRAILING_EMA[ticker])
            smoothed_bid = TRAILING_EMA[ticker]

            # Track EMA update count for warmup gating
            EMA_UPDATE_COUNT[ticker] = EMA_UPDATE_COUNT.get(ticker, 0) + 1

            # === 2. Update trailing peak (highest smoothed value seen) ===
            current_smoothed_value = contracts * smoothed_bid
            if ticker not in TRAILING_PEAK or current_smoothed_value > TRAILING_PEAK[ticker]:
                TRAILING_PEAK[ticker] = current_smoothed_value
    else:
        smoothed_bid = current_price
        current_smoothed_value = contracts * smoothed_bid

    # === 3. Edge erosion: tighter stop for expensive contracts ===
    # The hard stop (70% of position value) is useless for high-entry contracts
    # because the bid must drop to nearly zero before triggering.
    # Edge erosion measures what fraction of the *profit potential* has been consumed
    # by the drawdown, using the EMA-smoothed bid to filter noise.
    #   edge = 1.00 - entry_price  (max profit per contract)
    #   erosion = entry_price - smoothed_bid  (how far the smoothed bid has dropped)
    #   erosion_ratio = erosion / edge
    # Gating:
    #   - EMA warmup: skip until >= 8 updates (≈16s) to avoid premature triggers from noisy first ticks
    #   - TTC gate: skip when ttc <= 120s — late_pnl_stop handles the final window
    #   - Proportional threshold: more lenient as market approaches close
    #     ttc > 30 min → config value (80%), 10-30 min → 90%, < 10 min → disabled
    edge_per_contract = 1.0 - entry_price
    if edge_per_contract >= POSITION_STOP_LOSS_EDGE_EROSION_MIN_EDGE and POSITION_STOP_LOSS_EDGE_EROSION_PCT > 0 and ticker:
        ema_updates = 0
        with _STATE_LOCK:
            ema_updates = EMA_UPDATE_COUNT.get(ticker, 0)

        # Gate 1: EMA warmup — need >= 8 updates for stable smoothing
        if ema_updates < 8:
            pass  # skip edge erosion — EMA not yet reliable
        # Gate 2: TTC — don't fire near settlement, let late_pnl_stop handle it
        elif seconds_to_close is not None and seconds_to_close <= 120:
            pass  # skip edge erosion — too close to settlement
        else:
            # Gate 3: Proportional threshold based on time-to-close
            if seconds_to_close is not None and seconds_to_close <= 600:
                # < 10 min: disable edge erosion entirely
                effective_erosion_pct = None
            elif seconds_to_close is not None and seconds_to_close <= 1800:
                # 10-30 min: more lenient threshold
                effective_erosion_pct = 0.90
            else:
                # > 30 min (or unknown ttc): use configured threshold
                effective_erosion_pct = POSITION_STOP_LOSS_EDGE_EROSION_PCT

            if effective_erosion_pct is not None:
                erosion = entry_price - smoothed_bid
                erosion_ratio = erosion / edge_per_contract if erosion > 0 else 0.0
                if erosion_ratio >= effective_erosion_pct:
                    trigger_bid = entry_price - (edge_per_contract * effective_erosion_pct)
                    reason_ee = (
                        f"edge erosion: EMA bid eroded {erosion_ratio:.1%} of profit edge "
                        f"(entry=${entry_price:.3f}, ema_bid=${smoothed_bid:.3f}, "
                        f"edge=${edge_per_contract:.3f}, erosion=${erosion:.3f}, "
                        f"threshold={effective_erosion_pct:.0%}, trigger_bid=${trigger_bid:.3f}, "
                        f"ema_updates={ema_updates})"
                    )
                    log_sl_event("edge_erosion_triggered", ticker=ticker, direction=direction,
                                 contracts=contracts, entry_price=entry_price, bid=current_price,
                                 ema_bid=round(smoothed_bid, 4), edge=round(edge_per_contract, 4),
                                 erosion=round(erosion, 4), erosion_ratio=round(erosion_ratio, 4),
                                 threshold=effective_erosion_pct, bid_size=bid_size,
                                 ema_updates=ema_updates,
                                 ttc=round(seconds_to_close, 1) if seconds_to_close is not None else None)
                    return True, "edge_erosion", reason_ee

    # === 3a. Late-stage PnL stop: time-weighted exit for mid-priced contracts ===
    # Data-driven from 57 15M BTC trades: catches 6/12 losses with 0/40 false triggers.
    # ttc<=30s + pnl<-8%  OR  ttc<=60s + pnl<-15%
    if POSITION_STOP_LOSS_LATE_PNL_ENABLED and seconds_to_close is not None and ticker:
        pnl_pct = ((current_price - entry_price - fee_per_contract) / entry_price * 100.0) if entry_price > 0 else 0.0
        late_trigger = False
        late_band = ""
        if seconds_to_close <= 30 and pnl_pct < -8.0:
            late_trigger = True
            late_band = "ttc<=30s / pnl<-8%"
        elif seconds_to_close <= 60 and pnl_pct < -15.0:
            late_trigger = True
            late_band = "ttc<=60s / pnl<-15%"
        if late_trigger:
            reason_lp = (
                f"late-stage PnL stop: {late_band} "
                f"(entry=${entry_price:.3f}, bid=${current_price:.3f}, "
                f"pnl_pct={pnl_pct:.1f}%, ttc={seconds_to_close:.0f}s)"
            )
            log_sl_event("late_pnl_stop_triggered", ticker=ticker, direction=direction,
                         contracts=contracts, entry_price=entry_price, bid=current_price,
                         pnl_pct=round(pnl_pct, 2), ttc=round(seconds_to_close, 1),
                         band=late_band, bid_size=bid_size)
            return True, "late_pnl_stop", reason_lp

    # === 3b. Bid stagnation exit: frozen orderbook while underwater ===
    # Data-driven: mark unchanged ±$0.01 for >=45s AND pnl<-3% AND ttc<=120s
    # Catches 5/12 losses (3 overlap with late-stage), 0/40 false triggers.
    if seconds_to_close is not None and ticker:
        now_mono = time.monotonic()
        with _STATE_LOCK:
            if ticker not in MARK_HISTORY:
                MARK_HISTORY[ticker] = deque(maxlen=60)
            MARK_HISTORY[ticker].append((now_mono, current_price))
            if seconds_to_close <= POSITION_STOP_LOSS_STAGNATION_TTC_MAX:
                pnl_pct_stag = ((current_price - entry_price - fee_per_contract) / entry_price * 100.0) if entry_price > 0 else 0.0
                if pnl_pct_stag < POSITION_STOP_LOSS_STAGNATION_PNL_PCT:
                    cutoff = now_mono - POSITION_STOP_LOSS_STAGNATION_SECONDS
                    history = list(MARK_HISTORY[ticker])
                    has_old = any(t <= cutoff for t, m in history)
                    if has_old:
                        window_marks = [(t, m) for t, m in history if t >= cutoff]
                        if window_marks and all(abs(m - current_price) <= 0.01 for t, m in window_marks):
                            span = now_mono - window_marks[0][0]
                            reason_stag = (
                                f"bid stagnation: mark frozen ±$0.01 for {span:.0f}s while underwater "
                                f"(entry=${entry_price:.3f}, mark=${current_price:.3f}, "
                                f"pnl_pct={pnl_pct_stag:.1f}%, ttc={seconds_to_close:.0f}s)"
                            )
                            log_sl_event("stagnation_exit_triggered", ticker=ticker, direction=direction,
                                         contracts=contracts, entry_price=entry_price, bid=current_price,
                                         pnl_pct=round(pnl_pct_stag, 2), ttc=round(seconds_to_close, 1),
                                         stagnation_seconds=round(span, 1), bid_size=bid_size)
                            return True, "bid_stagnation", reason_stag

    # === 4. Hard stop: configured % loss on original position value ===
    # Uses raw price (not EMA) — sustained wait handles noise filtering
    if position_value > 0 and unrealized_pnl <= -(position_value * POSITION_STOP_LOSS_PERCENT):
        reason = (
            f"hard stop: position lost {POSITION_STOP_LOSS_PERCENT:.0%}+ of entry value "
            f"(entry_value=${position_value:.2f}, pnl=${unrealized_pnl:.2f}, "
            f"entry=${entry_price:.3f}, bid=${current_price:.3f})"
        )
        log_sl_event("hard_stop_triggered", ticker=ticker, direction=direction,
                     contracts=contracts, entry_price=entry_price, bid=current_price,
                     pnl=round(unrealized_pnl, 4), position_value=round(position_value, 4),
                     threshold=POSITION_STOP_LOSS_PERCENT, bid_size=bid_size,
                     ema=round(smoothed_bid, 4) if ticker else None)
        return True, "stop_loss", reason

    # === 5. Momentum reversal for longer-duration markets ===
    if ticker:
        is_short_market = any(x in ticker for x in ["15M", "5M", "10M"])
        if not is_short_market:
            with _STATE_LOCK:
                peak_value = TRAILING_PEAK.get(ticker)
            if peak_value is not None and peak_value > 0:
                drop_from_peak = (peak_value - current_smoothed_value) / peak_value

                # Track bid sizes for shrinking detection
                with _STATE_LOCK:
                    if ticker not in RECENT_BID_SIZES:
                        RECENT_BID_SIZES[ticker] = deque(maxlen=8)
                    RECENT_BID_SIZES[ticker].append(bid_size)
                    recent_list = list(RECENT_BID_SIZES[ticker])

                bid_shrinking = False
                if len(recent_list) >= 6:
                    avg_bid_size = sum(recent_list) / len(recent_list)
                    # Require meaningful average liquidity — low avg means illiquid, not reversing
                    if avg_bid_size >= 50:
                        current_is_much_lower = bid_size <= 0.60 * avg_bid_size
                        downward_trend = (len(recent_list) >= 3 and
                                         recent_list[-3] > recent_list[-2] > recent_list[-1])
                        bid_shrinking = current_is_much_lower and downward_trend

                if drop_from_peak >= POSITION_STOP_LOSS_MOMENTUM_DROP and bid_shrinking:
                    reason_mr = (
                        f"momentum reversal: {drop_from_peak:.1%} drop from peak with shrinking bids "
                        f"(peak_value=${peak_value:.2f}, current=${current_smoothed_value:.2f}, "
                        f"bid_size={bid_size}, recent_avg={sum(recent_list)/len(recent_list):.0f})"
                    )
                    log_sl_event("momentum_reversal_triggered", ticker=ticker, direction=direction,
                                 contracts=contracts, entry_price=entry_price, bid=current_price,
                                 ema=round(smoothed_bid, 4), peak_value=round(peak_value, 2),
                                 current_smoothed=round(current_smoothed_value, 2),
                                 drop_pct=round(drop_from_peak, 4), bid_size=bid_size,
                                 recent_bids=recent_list)
                    return True, "momentum_reversal", reason_mr

    return False, None, None


# --- WebSocket orderbook state ---
# Full orderbook per ticker: { ticker: { "yes": {price: size, ...}, "no": {price: size, ...} } }
WS_ORDERBOOKS = {}
_BOOK_LOCK = threading.Lock()

def _book_to_sorted_list(book_dict):
    """Convert {price: size} dict → [[price, size], ...] sorted ascending by price."""
    return sorted([[p, s] for p, s in book_dict.items() if s > 0], key=lambda x: x[0])


def handle_orderbook_ws(ticker, msg_type, data):
    """Handle orderbook_snapshot and orderbook_delta from Kalshi WS.

    Kalshi WS format:
      Snapshot: { yes_dollars_fp: [[price, size], ...], no_dollars_fp: [[price, size], ...] }
      Delta:    { price_dollars: "0.95", delta_fp: "100.00", side: "yes"|"no" }

    Snapshots replace the entire book.  Deltas merge into the existing book
    (size=0 or negative delta removes a price level).
    """
    if msg_type == "orderbook_snapshot":
        # Kalshi uses yes_dollars_fp / no_dollars_fp for snapshots
        yes_levels = data.get("yes_dollars_fp", data.get("yes", []))
        no_levels = data.get("no_dollars_fp", data.get("no", []))
        yes_book = {float(p): float(s) for p, s in yes_levels} if yes_levels else {}
        no_book = {float(p): float(s) for p, s in no_levels} if no_levels else {}
        with _BOOK_LOCK:
            WS_ORDERBOOKS[ticker] = {"yes": yes_book, "no": no_book}
    else:
        # Delta: single price level update with side, price_dollars, delta_fp
        side = data.get("side", "")
        price_str = data.get("price_dollars")
        delta_str = data.get("delta_fp")

        if price_str is not None and delta_str is not None and side in ("yes", "no"):
            price = float(price_str)
            delta = float(delta_str)
            with _BOOK_LOCK:
                existing = WS_ORDERBOOKS.get(ticker, {"yes": {}, "no": {}})
                current_size = existing[side].get(price, 0.0)
                new_size = current_size + delta
                if new_size <= 0:
                    existing[side].pop(price, None)
                else:
                    existing[side][price] = new_size
                WS_ORDERBOOKS[ticker] = existing

    # === Orderbook momentum guard: detect large opposite-side delta spikes ===
    # Only count POSITIVE deltas near the top of the book (within 10¢ of best bid).
    # Positive delta = new bids placed (real pressure). Negative = cancellations (not pressure).
    # Bottom-of-book activity (e.g. 51K contracts at $0.01) is cleanup, not pressure.
    if msg_type == "orderbook_delta":
        side = data.get("side", "")
        raw_delta = float(data.get("delta_fp", 0))
        delta_price = float(data.get("price_dollars", 0))
        if side in ("yes", "no") and raw_delta >= MOMENTUM_GUARD_DELTA_FLOOR:
            # Check if delta is near the best bid (top of book)
            with _BOOK_LOCK:
                book = WS_ORDERBOOKS.get(ticker, {}).get(side, {})
                best_price = max(book.keys()) if book else 0
                best_bid_size = book.get(best_price, 0) if best_price else 0
            near_top = best_price > 0 and (best_price - delta_price) <= 0.10
            dynamic_threshold = max(MOMENTUM_GUARD_DELTA_FLOOR, best_bid_size * MOMENTUM_GUARD_BEST_BID_RATIO)
            if near_top and raw_delta >= dynamic_threshold:
                with _STATE_LOCK:
                    OPPOSITE_PRESSURE_SIGNALS[ticker] = {
                        "side": side,
                        "delta": raw_delta,
                        "time": time.monotonic(),
                    }
                logger.info(
                    f"[MOMENTUM_GUARD] Large {side.upper()} delta on {ticker} at ${delta_price:.2f}: "
                    f"+{raw_delta:.0f} contracts (best=${best_price:.2f}x{best_bid_size:.0f}, "
                    f"threshold={dynamic_threshold:.0f} = max({MOMENTUM_GUARD_DELTA_FLOOR}, {MOMENTUM_GUARD_BEST_BID_RATIO:.0%}x{best_bid_size:.0f}))"
                )

    # Build sorted arrays and push to REALTIME_QUOTES
    with _BOOK_LOCK:
        book = WS_ORDERBOOKS.get(ticker, {"yes": {}, "no": {}})
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
    if msg_type == "orderbook_delta":
        side = data.get("side", "?")
        price_str = data.get("price_dollars", "?")
        delta_str = data.get("delta_fp", "?")
        parts.append(f"change={side.upper()[0]}${price_str}→Δ{delta_str}")
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
PENDING_DISCORD_APPROVALS = {}  # ticker -> True, tracks tickers awaiting Discord approval
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


def _execute_exit(ticker, direction, contracts, entry_price, entry_fees, trigger, exit_reason, close_key):
    """Execute an exit order for a position. Thread-safe — opens its own DB connection."""
    try:
        if trigger == "stop_loss":
            slippage_schedule = [0.02, 0.04, 0.06]
        else:
            slippage_schedule = [0.01, 0.02, 0.04]

        now_ts = time.time()
        order_filled = False
        fresh_quote = None
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
                time.sleep(0.3)
                continue

            order_filled = True
            break

        if not order_filled:
            PENDING_CLOSE_UNTIL.pop(close_key, None)
            log_sl_event("exit_failed", ticker=ticker, trigger=trigger, direction=direction,
                         contracts=contracts, entry_price=entry_price,
                         reason="all_ioc_attempts_failed")
            return

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
            f"[EXIT] CLOSED: {ticker} | trigger={trigger} | dir={direction} | contracts={contracts} | "
            f"entry=${entry_price:.4f} | exit=${exit_price:.4f} | fees=${entry_fees + exit_fees:.2f} | "
            f"pnl=${realized_pnl:.2f} ({realized_pnl_pct:.2f}%) | order={json.dumps(order_body)}"
        )
        log_sl_event("exit_executed", ticker=ticker, trigger=trigger, direction=direction,
                     contracts=contracts, entry_price=entry_price, exit_price=round(exit_price, 4),
                     entry_fees=round(entry_fees, 4), exit_fees=round(exit_fees, 4),
                     realized_pnl=round(realized_pnl, 4), realized_pnl_pct=round(realized_pnl_pct, 4),
                     order_status=order_status, reason=exit_reason)

        # Own DB connection for thread safety
        conn = sqlite3.connect("trades.db", timeout=5)
        cursor = conn.cursor()
        try:
            rows_closed = mark_fills_closed(cursor, ticker, exit_price, trigger)
            conn.commit()
            logger.info(f"Marked {rows_closed} OPEN fill(s) as CLOSED for {ticker} at exit={exit_price:.4f}")
        finally:
            conn.close()

        # Clean state for closed position
        with _STATE_LOCK:
            STOP_LOSS_BREACH_START.pop(ticker, None)
            TRAILING_EMA.pop(ticker, None)
            TRAILING_PEAK.pop(ticker, None)
            RECENT_BID_SIZES.pop(ticker, None)
            OPPOSITE_PRESSURE_SIGNALS.pop(ticker, None)
            EMA_UPDATE_COUNT.pop(ticker, None)
            # Record SL exit cooldown so other bots can check before re-entering
            SL_EXIT_COOLDOWN[ticker] = time.monotonic()

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
        PENDING_CLOSE_UNTIL.pop(close_key, None)
        response = getattr(close_err, 'response', None)
        status_code = getattr(response, 'status_code', 'unknown')
        response_body = (getattr(response, 'text', '') or '')[:1000]
        logger.error(
            f"Close order failed for {ticker}: {close_err} | status={status_code} | body={response_body or 'N/A'}"
        )


def monitor_positions_once():
    """Monitor live positions and submit IOC close orders at configured P&L % thresholds."""
    # Check Discord remote pause flag
    if discord_bot.is_paused("monitor"):
        logger.info("[MONITOR] Position monitor is PAUSED via Discord — skipping cycle")
        return

    logger.info("Checking open positions for stop-loss exits...")

    positions = get_current_positions()

    conn = get_db_connection()
    cursor = conn.cursor()

    # --- Reconcile: close DB fills for tickers with zero exchange position ---
    api_tickers = {p.get('ticker') for p in positions if p.get('ticker')} if positions else set()
    cursor.execute("SELECT DISTINCT market_ticker FROM trades WHERE status = 'OPEN'")
    db_open_tickers = {row[0] for row in cursor.fetchall()}
    stale_tickers = db_open_tickers - api_tickers - RECONCILED_SETTLED_TICKERS
    for stale_ticker in stale_tickers:
        # Tag trades that had no prior exit mechanism as held_to_settlement
        cursor.execute(
            """
            UPDATE trades
            SET status = 'SETTLED',
                reason = reason || CASE
                    WHEN reason NOT LIKE '%[EXIT]%' THEN ' | [EXIT] held_to_settlement'
                    ELSE ''
                END || ' | auto-settled (zero exchange position)',
                resolved_timestamp = datetime('now')
            WHERE market_ticker = ? AND status = 'OPEN'
            """,
            (stale_ticker,),
        )
        rows_closed = cursor.rowcount
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
                logger.error(f"Failed to dynamically subscribe to ticker {ticker}: {e}")

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
        bid_size = int(executable_quote.get("size", 0))
        is_inferred_quote = "inferred" in executable_quote.get("source", "")

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

        # Smart exit decision (hard stop + EMA momentum reversal)
        should_exit, trigger, exit_reason = compute_smart_exit(
            entry_price, current_price, direction, seconds_to_close, market_duration_seconds,
            fee_per_contract=fee_per_contract, contracts=contracts,
            bid_size=bid_size, ticker=ticker
        )

        # Stop-loss breach timing: sustained breach or severe → auto-execute
        # Momentum reversal & edge erosion require 15s sustained wait to filter flash crashes
        auto_execute_sl = False
        if should_exit and trigger in ("stop_loss", "momentum_reversal", "edge_erosion", "late_pnl_stop", "bid_stagnation"):
            total_fees = fee_per_contract * contracts
            position_value = contracts * entry_price
            raw_pnl = contracts * (current_price - entry_price) - total_fees

            if trigger == "late_pnl_stop":
                # Late PnL stop: near expiration + deep loss — bypass liquidity gate entirely.
                # Even a bad fill or failed IOC is better than riding to settlement.
                logger.warning(
                    f"[EXIT] LATE_PNL_STOP on {ticker} — bypassing liquidity gate "
                    f"(bid_size={bid_size}, ttc={seconds_to_close:.0f}s, pnl={raw_pnl:.2f})"
                )
                log_sl_event("late_pnl_bypass_liquidity", ticker=ticker, direction=direction,
                             contracts=contracts, entry_price=entry_price, bid=current_price,
                             pnl=round(raw_pnl, 4), ttc=round(seconds_to_close, 1) if seconds_to_close else 0,
                             bid_size=bid_size)
                auto_execute_sl = True
            elif trigger in ("stop_loss", "edge_erosion", "bid_stagnation", "momentum_reversal"):
                # Severity check: configured severe % → exit immediately (hard stop only)
                severe_breach = (
                    trigger == "stop_loss"
                    and position_value > 0
                    and raw_pnl <= -(position_value * POSITION_STOP_LOSS_SEVERE_PERCENT)
                )

                if severe_breach:
                    if bid_size >= POSITION_STOP_LOSS_MIN_BID_SIZE or is_inferred_quote:
                        logger.warning(
                            f"[EXIT] SEVERE: {ticker} | pnl=${raw_pnl:.2f} vs entry_value=${position_value:.2f} — "
                            f"skipping wait, exiting immediately{' (inferred quote)' if is_inferred_quote else ''}"
                        )
                        log_sl_event("severe_breach_exit", ticker=ticker, direction=direction,
                                     contracts=contracts, entry_price=entry_price, bid=current_price,
                                     pnl=round(raw_pnl, 4), position_value=round(position_value, 4),
                                     severe_pct=POSITION_STOP_LOSS_SEVERE_PERCENT, bid_size=bid_size,
                                     inferred=is_inferred_quote)
                        exit_reason = f"{exit_reason} (severe breach — immediate exit)"
                        auto_execute_sl = True
                    else:
                        logger.warning(
                            f"[EXIT] SEVERE breach on {ticker} but low liquidity "
                            f"(bid_size={bid_size} < min={POSITION_STOP_LOSS_MIN_BID_SIZE}) — holding"
                        )
                        log_sl_event("severe_breach_blocked", ticker=ticker, reason="low_liquidity",
                                     bid_size=bid_size, min_bid_size=POSITION_STOP_LOSS_MIN_BID_SIZE,
                                     pnl=round(raw_pnl, 4))
                        should_exit = False
                        trigger = None
                        exit_reason = None
                else:
                    # Normal breach: require sustained duration
                    # Check for opposite-side orderbook pressure to accelerate
                    now_mono = time.monotonic()
                    opposite_side = "no" if direction == "YES" else "yes"
                    has_pressure = False
                    with _STATE_LOCK:
                        if ticker not in STOP_LOSS_BREACH_START:
                            STOP_LOSS_BREACH_START[ticker] = now_mono
                        breach_elapsed = now_mono - STOP_LOSS_BREACH_START[ticker]
                        sig = OPPOSITE_PRESSURE_SIGNALS.get(ticker)
                        if sig and sig["side"] == opposite_side:
                            if (now_mono - sig["time"]) <= MOMENTUM_GUARD_WINDOW_SECONDS:
                                has_pressure = True
                    effective_wait = MOMENTUM_GUARD_ACCELERATED_WAIT if has_pressure else POSITION_STOP_LOSS_SUSTAINED_SECONDS
                    # Edge erosion & momentum reversal get a longer sustained wait (15s) to filter flash crashes.
                    # Flash bid drops often recover within seconds; requiring 15s of sustained
                    # breach before executing prevents false exits from transient liquidity gaps.
                    if trigger in ("edge_erosion", "momentum_reversal") and not has_pressure:
                        effective_wait = max(effective_wait, 15.0)
                    if breach_elapsed < effective_wait:
                        pressure_tag = f" [MOMENTUM_GUARD: {opposite_side.upper()} pressure → wait={effective_wait:.0f}s]" if has_pressure else ""
                        logger.warning(
                            f"[EXIT] BREACH_WAIT: {ticker} | stop-loss breach for {breach_elapsed:.1f}s / "
                            f"{effective_wait:.0f}s | {exit_reason}{pressure_tag} — waiting"
                        )
                        log_sl_event("breach_waiting", ticker=ticker, direction=direction,
                                     contracts=contracts, entry_price=entry_price, bid=current_price,
                                     pnl=round(raw_pnl, 4), breach_elapsed=round(breach_elapsed, 2),
                                     effective_wait=effective_wait, has_pressure=has_pressure,
                                     pressure_side=opposite_side if has_pressure else None,
                                     bid_size=bid_size)
                        should_exit = False
                        trigger = None
                        exit_reason = None
                    else:
                        if bid_size >= POSITION_STOP_LOSS_MIN_BID_SIZE or is_inferred_quote:
                            pressure_note = f" [MOMENTUM_GUARD accelerated from {POSITION_STOP_LOSS_SUSTAINED_SECONDS:.0f}s→{effective_wait:.0f}s]" if has_pressure else ""
                            log_sl_event("breach_sustained_exit", ticker=ticker, direction=direction,
                                         contracts=contracts, entry_price=entry_price, bid=current_price,
                                         pnl=round(raw_pnl, 4), breach_elapsed=round(breach_elapsed, 2),
                                         effective_wait=effective_wait, has_pressure=has_pressure,
                                         pressure_side=opposite_side if has_pressure else None,
                                         bid_size=bid_size)
                            exit_reason = f"{exit_reason} (sustained {breach_elapsed:.1f}s{pressure_note} — auto-executing)"
                            auto_execute_sl = True
                        else:
                            logger.warning(
                                f"[EXIT] Stop-loss sustained on {ticker} but low liquidity "
                                f"(bid_size={bid_size} < min={POSITION_STOP_LOSS_MIN_BID_SIZE}) — holding"
                            )
                            log_sl_event("breach_sustained_blocked", ticker=ticker, reason="low_liquidity",
                                         bid_size=bid_size, min_bid_size=POSITION_STOP_LOSS_MIN_BID_SIZE,
                                         breach_elapsed=round(breach_elapsed, 2), pnl=round(raw_pnl, 4))
                            should_exit = False
                            trigger = None
                            exit_reason = None
        elif trigger is None:
            # No exit triggered — reset breach timer
            with _STATE_LOCK:
                STOP_LOSS_BREACH_START.pop(ticker, None)

        ttc_str = f"{seconds_to_close:.0f}s" if seconds_to_close is not None else "unknown"

        logger.info(
            f"{ticker} | dir={direction} | contracts={contracts} | entry={entry_price:.3f} | "
            f"entry_source={entry_source} | mark={current_price:.3f} | quote_source={executable_quote['source']} | "
            f"unrealized_pnl=${unrealized_pnl:.2f} | pnl_pct={unrealized_pnl_pct:.2f}% | "
            f"ttc={ttc_str} | exit={'YES:'+trigger if should_exit else 'NO'}"
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
            f"[EXIT] TRIGGER: {ticker} | {trigger} | dir={direction} | contracts={contracts} | "
            f"entry=${entry_price:.3f} | mark=${current_price:.3f} | pnl=${unrealized_pnl:.2f} ({unrealized_pnl_pct:.2f}%) | "
            f"reason={exit_reason}"
        )

        # --- Auto-execute stop-loss / momentum reversal (no approval needed) ---
        if auto_execute_sl:
            trigger_label = {"stop_loss": "stop-loss", "momentum_reversal": "momentum-reversal", "edge_erosion": "edge-erosion", "late_pnl_stop": "late-pnl-stop", "bid_stagnation": "bid-stagnation"}.get(trigger, trigger)
            logger.warning(f"[EXIT] AUTO-EXECUTING {trigger_label} for {ticker} — no Discord approval required")
            # Notify Discord (info only, not an approval request)
            if discord_bot.is_configured():
                discord_bot.send_exit_result(
                    ticker=ticker,
                    trigger=trigger,
                    direction=direction,
                    contracts=contracts,
                    entry_price=entry_price,
                    exit_price=current_price,
                    pnl=unrealized_pnl,
                    status="AUTO_SL",
                )
            _execute_exit(ticker, direction, contracts, entry_price, entry_fees, trigger, exit_reason, close_key)
            continue

        # --- Interactive Discord approval (non-blocking) ---
        if DISCORD_INTERACTIVE_SL_TP and discord_bot.is_configured():
            # If this ticker is already pending approval, skip (don't double-send)
            if ticker in PENDING_DISCORD_APPROVALS:
                logger.debug(f"[EXIT] Already pending Discord approval for {ticker}, skipping")
                continue
            ttc_for_discord = int(seconds_to_close) if seconds_to_close is not None else None
            msg_id = discord_bot.send_approval_request(
                ticker=ticker,
                trigger=trigger,
                direction=direction,
                contracts=contracts,
                entry_price=entry_price,
                current_price=current_price,
                unrealized_pnl=unrealized_pnl,
                pnl_pct=unrealized_pnl_pct,
                reason=exit_reason or "",
                ttc_seconds=ttc_for_discord,
            )
            if msg_id:
                # Launch approval polling in background thread so monitor loop continues
                PENDING_DISCORD_APPROVALS[ticker] = True
                PENDING_CLOSE_UNTIL[close_key] = time.time() + DISCORD_APPROVAL_TIMEOUT_SECONDS + 5
                def _run_approval(t=ticker, d=direction, c=contracts, ep=entry_price,
                                  ef=entry_fees, tr=trigger, er=exit_reason, mid=msg_id, ck=close_key):
                    try:
                        result = discord_bot.wait_for_approval(mid)
                        if result == "approved":
                            logger.info(f"[EXIT] APPROVED by user: {t} | {tr}")
                            _execute_exit(t, d, c, ep, ef, tr, er, ck)
                        else:
                            logger.info(f"[EXIT] {'REJECTED by user' if result == 'rejected' else 'NO RESPONSE (timeout)'} — holding: {t} | {tr}")
                            with _STATE_LOCK:
                                STOP_LOSS_BREACH_START.pop(t, None)
                            PENDING_CLOSE_UNTIL.pop(ck, None)
                    except Exception as e:
                        logger.error(f"[EXIT] Discord approval thread error for {t}: {e}")
                        PENDING_CLOSE_UNTIL.pop(ck, None)
                    finally:
                        PENDING_DISCORD_APPROVALS.pop(t, None)
                threading.Thread(target=_run_approval, daemon=True).start()
                continue
            else:
                logger.error(f"[EXIT] BLOCKED: Discord approval request failed to send for {ticker} — will NOT auto-execute. Retrying next cycle.")
                continue

        _execute_exit(ticker, direction, contracts, entry_price, entry_fees, trigger, exit_reason, close_key)

    # Clean up stale tracking state for positions no longer open
    open_tickers = {p.get("ticker") for p in positions if p.get("ticker")}
    with _STATE_LOCK:
        for d in [TRAILING_EMA, TRAILING_PEAK, STOP_LOSS_BREACH_START, RECENT_BID_SIZES, OPPOSITE_PRESSURE_SIGNALS, MARK_HISTORY, EMA_UPDATE_COUNT]:
            for k in list(d.keys()):
                if k not in open_tickers:
                    d.pop(k, None)
        # Clean expired SL cooldowns (> 10 minutes old)
        now_mono = time.monotonic()
        for k in list(SL_EXIT_COOLDOWN.keys()):
            if now_mono - SL_EXIT_COOLDOWN[k] > 600:
                SL_EXIT_COOLDOWN.pop(k, None)

    conn.close()

def monitor_positions():
    """Run the exit monitor loop continuously."""
    logger.info(
        f"Starting position monitor loop | exit_strategy=smart_binary | "
        f"interval={MONITOR_INTERVAL_SECONDS}s | "
        f"stop_loss={'enabled' if POSITION_STOP_LOSS_ENABLED else 'DISABLED'} | "
        f"sl_threshold={POSITION_STOP_LOSS_PERCENT:.0%} | severe={POSITION_STOP_LOSS_SEVERE_PERCENT:.0%} | "
        f"edge_erosion={POSITION_STOP_LOSS_EDGE_EROSION_PCT:.0%} | "
        f"late_pnl={'enabled' if POSITION_STOP_LOSS_LATE_PNL_ENABLED else 'DISABLED'} (30s/-8%, 60s/-15%) | "
        f"stagnation={POSITION_STOP_LOSS_STAGNATION_SECONDS:.0f}s / pnl<{POSITION_STOP_LOSS_STAGNATION_PNL_PCT}% / ttc<={POSITION_STOP_LOSS_STAGNATION_TTC_MAX}s | "
        f"sustained={POSITION_STOP_LOSS_SUSTAINED_SECONDS}s | momentum_drop={POSITION_STOP_LOSS_MOMENTUM_DROP:.0%} | "
        f"min_bid_size={POSITION_STOP_LOSS_MIN_BID_SIZE} | "
        f"hold_for_settlement={SETTLEMENT_HOLD_ENABLED} | settlement_window={SETTLEMENT_HOLD_SECONDS}s"
    )
    while True:
        try:
            monitor_positions_once()
        except Exception as e:
            logger.error(f"Unexpected monitor loop error: {e}")
        time.sleep(MONITOR_INTERVAL_SECONDS)

if __name__ == "__main__":
    # Start Discord command listener for remote control (silent — execution_bot sends replies)
    discord_bot.start_command_listener(respond=False)

    # Pre-cache Discord bot user ID to avoid latency on first approval
    if DISCORD_INTERACTIVE_SL_TP and discord_bot.is_configured():
        discord_bot._get_bot_user_id()
        logger.info(f"[DISCORD_BOT] Pre-cached bot user ID: {discord_bot._BOT_USER_ID}")

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