"""Orderbook Edge Scanner — Continuous WebSocket-driven entry scanner.

Monitors real-time orderbook flow for candidate markets and identifies favorable
entry conditions based on depth imbalance, spread quality, top-of-book conviction,
and recent order flow. When conditions are met, runs the full decision engine
pipeline (including Grok validation) and places IOC buy orders.

Architecture:
  - Background WS thread: receives orderbook snapshots/deltas, updates local state
  - Main thread: periodically refreshes candidates via REST, evaluates edge scores,
    and triggers entries when score >= threshold

Run:  python orderbook_edge_scanner.py
"""

import time
import sqlite3
import requests
import uuid
import base64
import json
import threading
import os
import asyncio
import winsound
from datetime import datetime, timezone
from collections import deque
from loguru import logger
from logging_setup import setup_log_file, setup_error_log, setup_trade_decision_log
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from config import *
from discord_notifications import notify_trade_executed, notify_error, notify_position_closed
import discord_bot
from kalshi_ws_client import KalshiWebSocketClient

setup_log_file("edge_scanner.log")
setup_error_log()
setup_trade_decision_log()

# ═══════════════════════════════════════════════════════════════════
# Scanner constants (promote to .env via config.py when tuned)
# ═══════════════════════════════════════════════════════════════════
SCANNER_EVAL_INTERVAL = 5          # seconds between score evaluations


def calculate_hours_to_close(close_time) -> float | None:
	"""Calculate hours remaining until market close."""
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
		return max(0.0, (close_timestamp - current_timestamp) / 3600.0)
	except (TypeError, ValueError, OverflowError):
		return None
MARKET_REFRESH_INTERVAL = 600      # seconds between REST market scans (10 min)
FLOW_WINDOW_SECONDS = 30           # look-back window for order-flow signal
MIN_DEPTH_CONTRACTS = 200          # minimum total resting depth on our side
MAX_SPREAD_CENTS = 5               # max bid-ask spread to consider for entry
MAX_CANDIDATES = 50                # cap on WS subscriptions

# ═══════════════════════════════════════════════════════════════════
# Global state
# ═══════════════════════════════════════════════════════════════════
BOOKS = {}          # ticker -> {"yes": {price: size}, "no": {price: size}}
QUOTES = {}         # ticker -> {"yes_dollars": [[p,s],...], "no_dollars": [...], "timestamp": dt}
FLOW = {}           # ticker -> deque of (monotonic_time, side_str, delta_val, price)
CANDIDATES = {}     # ticker -> {"market": dict, "direction": str, ...}
LAST_ATTEMPT = {}   # ticker -> time.monotonic() of last trade attempt
SL_WATCH_TICKERS = set()  # tickers with open positions being monitored for stop loss
_BOOK_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
WS_CLIENT = None

# ═══════════════════════════════════════════════════════════════════
# Kalshi API client (same pattern as execution_bot / position_monitor)
# ═══════════════════════════════════════════════════════════════════
host = "https://demo-api.kalshi.co" if MODE == "demo" else "https://api.elections.kalshi.com"
api_prefix = "/trade-api/v2"

private_key = None
try:
	with open(KALSHI_PRIVATE_KEY_PATH, "rb") as key_file:
		private_key = serialization.load_pem_private_key(
			key_file.read(), password=None, backend=default_backend()
		)
	logger.info("[EDGE] Private key loaded successfully")
except Exception as e:
	logger.error(f"[EDGE] Failed to load private key: {e}")
	raise


def create_signature(private_key, timestamp, method, path):
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
	try:
		resp = signed_request('GET', '/portfolio/fills', params={'order_id': order_id})
		fills = resp.get('fills', [])
		if not fills:
			return None, 0.0
		total_value = total_count = total_fees = 0.0
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
		logger.warning(f"[EDGE] Failed to fetch fills for {order_id}: {e}")
		return None, 0.0


# ═══════════════════════════════════════════════════════════════════
# Database helpers
# ═══════════════════════════════════════════════════════════════════
def get_db_connection():
	conn = sqlite3.connect('trades.db', timeout=5)
	conn.execute("PRAGMA busy_timeout = 5000")
	conn.execute("PRAGMA journal_mode=WAL")
	return conn


def get_daily_trade_count():
	conn = get_db_connection()
	cur = conn.cursor()
	cur.execute(
		"SELECT COUNT(*) FROM trades WHERE timestamp >= date('now') AND status != 'CANCELED'"
	)
	count = cur.fetchone()[0]
	conn.close()
	return count


def get_recent_sl_exits(minutes=5):
	"""Return set of tickers that had a stop-loss exit in the last N minutes."""
	conn = get_db_connection()
	cur = conn.cursor()
	cur.execute(
		"SELECT DISTINCT market_ticker FROM trades "
		"WHERE status = 'CLOSED' "
		"AND reason LIKE '%closed by %' "
		"AND resolved_timestamp >= datetime('now', ?)",
		(f'-{minutes} minutes',),
	)
	tickers = {row[0] for row in cur.fetchall()}
	conn.close()
	return tickers


# ═══════════════════════════════════════════════════════════════════
# WebSocket orderbook handler
# ═══════════════════════════════════════════════════════════════════
def handle_orderbook(ticker, msg_type, data):
	"""WS callback: update local book state and track order flow."""
	if msg_type == "orderbook_snapshot":
		yes_levels = data.get("yes_dollars_fp", data.get("yes", []))
		no_levels = data.get("no_dollars_fp", data.get("no", []))
		yes_book = {float(p): float(s) for p, s in yes_levels} if yes_levels else {}
		no_book = {float(p): float(s) for p, s in no_levels} if no_levels else {}
		with _BOOK_LOCK:
			BOOKS[ticker] = {"yes": yes_book, "no": no_book}

	elif msg_type == "orderbook_delta":
		side = data.get("side", "")
		price_str = data.get("price_dollars")
		delta_str = data.get("delta_fp")
		if price_str is not None and delta_str is not None and side in ("yes", "no"):
			price = float(price_str)
			delta = float(delta_str)
			with _BOOK_LOCK:
				existing = BOOKS.get(ticker, {"yes": {}, "no": {}})
				current_size = existing[side].get(price, 0.0)
				new_size = current_size + delta
				if new_size <= 0:
					existing[side].pop(price, None)
				else:
					existing[side][price] = new_size
				BOOKS[ticker] = existing

			# Track positive deltas near top-of-book as flow signals
			if delta > 0:
				with _BOOK_LOCK:
					book_side = BOOKS.get(ticker, {}).get(side, {})
					best_price = max(book_side.keys()) if book_side else 0
				near_top = best_price > 0 and (best_price - price) <= 0.10
				if near_top:
					with _STATE_LOCK:
						if ticker not in FLOW:
							FLOW[ticker] = deque(maxlen=200)
						FLOW[ticker].append((time.monotonic(), side, delta, price))

	# Rebuild sorted arrays for quote access
	with _BOOK_LOCK:
		book = BOOKS.get(ticker, {"yes": {}, "no": {}})
		yes_sorted = sorted(
			[[p, s] for p, s in book.get("yes", {}).items() if s > 0],
			key=lambda x: x[0],
		)
		no_sorted = sorted(
			[[p, s] for p, s in book.get("no", {}).items() if s > 0],
			key=lambda x: x[0],
		)
		QUOTES[ticker] = {
			"yes_dollars": yes_sorted,
			"no_dollars": no_sorted,
			"timestamp": datetime.now(timezone.utc),
		}


# ═══════════════════════════════════════════════════════════════════
# Edge scoring
# ═══════════════════════════════════════════════════════════════════
def compute_edge_score(ticker, direction):
	"""Score the current orderbook for entry quality (0–100).

	Components:
	  Depth imbalance  (0-35): our side's share of total resting depth
	  Spread quality   (0-25): tighter spread = better entry
	  Top-of-book      (0-20): large resting bid at best price = support
	  Order flow       (0-20): recent positive deltas on our side

	Returns (score, details_dict).
	"""
	with _BOOK_LOCK:
		book = BOOKS.get(ticker)
		quote = QUOTES.get(ticker)
	if not book or not quote:
		return 0, {"reason": "no_data"}

	age = (datetime.now(timezone.utc) - quote["timestamp"]).total_seconds()
	if age > 30:
		return 0, {"reason": f"stale_{age:.0f}s"}

	yes_book = book.get("yes", {})
	no_book = book.get("no", {})

	if direction == "YES":
		our_book, opp_book = yes_book, no_book
	else:
		our_book, opp_book = no_book, yes_book

	if not our_book:
		return 0, {"reason": "empty_our_side"}

	# --- Depth ---
	our_depth = sum(our_book.values())
	opp_depth = sum(opp_book.values()) if opp_book else 0
	total_depth = our_depth + opp_depth

	if our_depth < MIN_DEPTH_CONTRACTS:
		return 0, {"reason": f"low_depth_{our_depth:.0f}"}

	# --- Best prices ---
	our_best_price = max(our_book.keys())
	our_best_size = our_book[our_best_price]
	opp_best_price = max(opp_book.keys()) if opp_book else 0

	# --- Spread ---
	if opp_best_price > 0:
		implied_ask = 1.0 - opp_best_price
		spread = implied_ask - our_best_price
	else:
		spread = 0.10
	spread_cents = max(0, int(round(spread * 100)))
	if spread_cents > MAX_SPREAD_CENTS:
		return 0, {"reason": f"wide_spread_{spread_cents}c"}

	# === Score components ===

	# 1. Depth imbalance (0-35)
	ratio = our_depth / total_depth if total_depth > 0 else 0.5
	imbalance_score = min(35.0, max(0.0, (ratio - 0.5) * 2.0) * 35.0)

	# 2. Spread quality (0-25): 1¢=25, 2¢=20, 3¢=15, 4¢=10, 5¢=5
	spread_score = max(0.0, 25.0 - max(0, spread_cents - 1) * 5.0)

	# 3. Top-of-book support (0-20): scales linearly from 50 to 500 contracts
	top_score = min(20.0, our_best_size / 25.0) if our_best_size >= 50 else 0.0

	# 4. Order flow (0-20): positive deltas on our side within FLOW_WINDOW
	now_mono = time.monotonic()
	our_side = "yes" if direction == "YES" else "no"
	with _STATE_LOCK:
		flow_deque = FLOW.get(ticker, deque())
		flow_total = sum(
			d for (t, s, d, _p) in flow_deque
			if s == our_side and (now_mono - t) <= FLOW_WINDOW_SECONDS
		)
	flow_score = min(20.0, flow_total / 50.0) if flow_total >= 100 else 0.0

	total = imbalance_score + spread_score + top_score + flow_score

	details = {
		"our_depth": int(our_depth),
		"opp_depth": int(opp_depth),
		"imbalance": round(ratio, 3),
		"imbalance_pts": round(imbalance_score, 1),
		"spread_cents": spread_cents,
		"spread_pts": round(spread_score, 1),
		"best_bid": our_best_price,
		"best_bid_size": int(our_best_size),
		"top_pts": round(top_score, 1),
		"flow_30s": int(flow_total),
		"flow_pts": round(flow_score, 1),
		"total": round(total, 1),
	}

	return total, details


# ═══════════════════════════════════════════════════════════════════
# Market scanning & candidate management
# ═══════════════════════════════════════════════════════════════════
def _norm_price(val):
	if val in (None, ""):
		return 0.0
	try:
		p = float(val)
	except (TypeError, ValueError):
		return 0.0
	if p > 1.0:
		p /= 100.0
	return max(0.0, min(1.0, p))


def _market_volume(market):
	for field in ('volume', 'volume_fp', 'volume_24h', 'volume_24h_fp'):
		val = market.get(field)
		if val not in (None, ""):
			try:
				return float(val)
			except (TypeError, ValueError):
				continue
	return 0.0


def _is_multivariate(market):
	ticker = str(market.get('ticker') or '')
	return bool(market.get('mve_collection_ticker')) or ticker.startswith('KXMVECROSSCATEGORY-')


def fetch_markets_rest():
	"""Fetch open markets closing within MARKET_SCAN_HOURS via REST API."""
	current_seconds = int(time.time())
	max_close_seconds = current_seconds + (MARKET_SCAN_HOURS * 3600)
	params = {
		"limit": 1000,
		"status": "open",
		"min_close_ts": current_seconds,
		"max_close_ts": max_close_seconds,
	}
	all_markets = []
	cursor = None
	page = 1
	while page <= OPEN_MARKETS_MAX_PAGES:
		if cursor:
			params["cursor"] = cursor
		try:
			data = signed_request("GET", "/markets", params=params)
		except requests.exceptions.HTTPError as e:
			if e.response is not None and e.response.status_code == 429:
				logger.warning("[EDGE] Rate limit fetching markets — sleeping 60s")
				time.sleep(60)
				continue
			raise
		markets_page = data.get('markets', [])
		all_markets.extend(markets_page)
		cursor = data.get('cursor')
		page += 1
		if not cursor:
			break
	return all_markets


def build_candidates(markets):
	"""Filter markets through internal model pre-screen. Returns candidate dict.

	In signal_log mode, skips the internal model entirely — accepts any market
	matching title/volume/timing filters and sets direction=None (scored both ways later).
	"""
	conn = get_db_connection()
	cur = conn.cursor()
	cur.execute("SELECT DISTINCT market_ticker FROM trades WHERE status = 'OPEN'")
	open_positions = {row[0] for row in cur.fetchall()}
	cur.execute(
		"SELECT event_ticker, COUNT(*) FROM trades "
		"WHERE event_ticker IS NOT NULL AND status != 'CANCELED' "
		"GROUP BY event_ticker HAVING COUNT(*) >= 2"
	)
	saturated_events = {row[0] for row in cur.fetchall()}
	conn.close()

	excluded = set(EXCLUDED_MARKET_TICKERS)
	title_terms = (
		[t.strip().lower() for t in MARKET_TITLE_CONTAINS.split(',') if t.strip()]
		if MARKET_TITLE_CONTAINS else []
	)
	candidates = {}
	for market in markets:
		ticker = market.get('ticker')
		if not ticker or _is_multivariate(market):
			continue
		if ticker.upper() in excluded or ticker in open_positions:
			continue
		event_ticker = market.get('event_ticker', '')
		if event_ticker and event_ticker in saturated_events:
			continue
		if title_terms:
			title_lower = str(market.get('title', '')).lower()
			if not any(t in title_lower for t in title_terms):
				continue

		volume = _market_volume(market)
		volume_min = EDGE_SCANNER_VOLUME_THRESHOLD
		if volume < volume_min:
			continue

		htc = calculate_hours_to_close(market.get('close_time'))
		if htc is None or htc < MIN_HOURS_TO_CLOSE:
			continue

		# Extract REST mid-prices for internal model pre-filter
		yes_bid = _norm_price(market.get('yes_bid_dollars', market.get('yes_bid')))
		yes_ask = _norm_price(market.get('yes_ask_dollars', market.get('yes_ask')))
		no_bid = _norm_price(market.get('no_bid_dollars', market.get('no_bid')))
		no_ask = _norm_price(market.get('no_ask_dollars', market.get('no_ask')))

		yes_price = (yes_bid + yes_ask) / 2 if yes_bid > 0 and yes_ask > 0 else (yes_bid or yes_ask)
		no_price = (no_bid + no_ask) / 2 if no_bid > 0 and no_ask > 0 else (no_bid or no_ask)

		if not yes_price and not no_price:
			continue
		if yes_price and not no_price:
			no_price = 1.0 - yes_price
		if no_price and not yes_price:
			yes_price = 1.0 - no_price

		# Accept all markets — direction determined by orderbook scoring in evaluate_candidates
		candidates[ticker] = {
			"market": market,
			"direction": None,   # scored both ways in evaluate_candidates
			"yes_price": yes_price,
			"no_price": no_price,
			"volume": volume,
		}

		if len(candidates) >= MAX_CANDIDATES:
			break

	return candidates


def refresh_markets():
	"""Fetch candidate markets via REST and manage WS subscriptions."""
	global WS_CLIENT
	logger.info(f"[EDGE] Refreshing candidates (scan_hours={MARKET_SCAN_HOURS})...")
	markets = fetch_markets_rest()
	new_candidates = build_candidates(markets)

	old_tickers = set(CANDIDATES.keys())
	new_tickers = set(new_candidates.keys())
	added = new_tickers - old_tickers
	removed = old_tickers - new_tickers

	with _STATE_LOCK:
		CANDIDATES.clear()
		CANDIDATES.update(new_candidates)
		for t in removed:
			if t not in SL_WATCH_TICKERS:
				FLOW.pop(t, None)
				LAST_ATTEMPT.pop(t, None)
	with _BOOK_LOCK:
		for t in removed:
			if t not in SL_WATCH_TICKERS:
				BOOKS.pop(t, None)
				QUOTES.pop(t, None)

	# Subscribe new tickers to WS
	if WS_CLIENT and added:
		loop = getattr(WS_CLIENT, '_loop', None)
		if loop and loop.is_running():
			for t in added:
				asyncio.run_coroutine_threadsafe(WS_CLIENT.subscribe_ticker(t), loop)
		else:
			logger.warning("[EDGE] WS loop not running — tickers queued for reconnect")

	logger.info(
		f"[EDGE] Candidates: {len(new_candidates)} | +{len(added)} -{len(removed)} | "
		f"tickers={sorted(new_candidates.keys())}"
	)


# ═══════════════════════════════════════════════════════════════════
# Entry execution
# ═══════════════════════════════════════════════════════════════════
def attempt_entry(ticker, direction, candidate, score, details):
	"""Place an IOC buy order based on edge score alone — no external model."""
	market = candidate["market"]

	# Derive live prices from WS book
	with _BOOK_LOCK:
		book = BOOKS.get(ticker, {})
		yes_book = book.get("yes", {})
		no_book = book.get("no", {})
	yes_best_bid = max(yes_book.keys()) if yes_book else 0
	no_best_bid = max(no_book.keys()) if no_book else 0

	if yes_best_bid <= 0 or no_best_bid <= 0:
		logger.warning(f"[EDGE] Incomplete book for {ticker}: yes_bid={yes_best_bid}, no_bid={no_best_bid}")
		return False

	yes_ask = 1.0 - no_best_bid
	no_ask = 1.0 - yes_best_bid

	volume = candidate.get("volume", 0)
	title = market.get('title') or market.get('event_ticker') or 'Unknown'

	logger.info(
		f"[EDGE] Attempting entry for {ticker} | dir={direction} | score={score:.0f} | "
		f"yes_bid=${yes_best_bid:.3f} no_bid=${no_best_bid:.3f} | {title[:80]}"
	)

	# Balance check — skip if cash < flat risk amount
	try:
		bal_data = signed_request("GET", "/portfolio/balance")
		cash = bal_data.get('balance', 0) / 100
		if cash < EDGE_SCANNER_RISK_FLAT:
			logger.warning(
				f"[EDGE] Cash ${cash:.2f} below ${EDGE_SCANNER_RISK_FLAT:.2f} — pausing trades, skipping {ticker}"
			)
			return False
	except Exception as e:
		logger.error(f"[EDGE] Balance check failed: {e}")
		return False

	# Build order — flat $ risk per trade from EDGE_SCANNER_RISK_FLAT
	side = "yes" if direction == "YES" else "no"
	contract_price = yes_ask if side == "yes" else no_ask
	SLIPPAGE_CENTS = 1
	limit_price_cents = min(99, int(contract_price * 100) + SLIPPAGE_CENTS)
	limit_price_dollars = limit_price_cents / 100.0

	risk_dollars = EDGE_SCANNER_RISK_FLAT
	count = int(risk_dollars / limit_price_dollars) if limit_price_dollars > 0 else 0

	if count <= 0:
		logger.warning(f"[EDGE] Zero contracts for {ticker} at ${limit_price_dollars:.4f}")
		return False

	total_cost = count * limit_price_dollars
	client_order_id = f"edge-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

	order_body = {
		"ticker": ticker,
		"side": side,
		"action": "buy",
		"count": count,
		"type": "limit",
		"time_in_force": "immediate_or_cancel",
		"post_only": False,
		"client_order_id": client_order_id,
	}
	if side == "yes":
		order_body["yes_price"] = limit_price_cents
	else:
		order_body["no_price"] = limit_price_cents

	try:
		order_response = signed_request("POST", "/portfolio/orders", body=order_body)
		order_data = (
			order_response.get('order', order_response)
			if isinstance(order_response, dict) else {}
		)
		kalshi_order_id = order_data.get('order_id') or order_data.get('id')
		order_status = (
			order_data.get('status')
			or (order_response.get('status') if isinstance(order_response, dict) else None)
		)
		fill_count = float(order_data.get('fill_count_fp', 0) or 0)
		actually_filled = fill_count > 0 and order_status != 'canceled'

		if not actually_filled:
			logger.warning(f"[EDGE] IOC order for {ticker} NOT filled (status={order_status})")
			return False

		# Fetch actual fill price
		actual_price, entry_fees = (
			fetch_fill_price_and_fees(kalshi_order_id, side)
			if kalshi_order_id else (None, 0.0)
		)
		if actual_price is not None:
			contract_price = actual_price
			total_cost = count * contract_price
		else:
			entry_fees = 0.0

		reason = (f"edge_scanner(score={score:.0f}, imbal={details.get('imbalance_pts',0):.0f}, "
		          f"spread={details.get('spread_cents',0)}c, entry=${contract_price:.3f})")

		logger.success(
			f"[EDGE] TRADE EXECUTED: {direction} on {ticker} | qty={count} | "
			f"price=${contract_price:.4f} | cost=${total_cost:.2f} | fees=${entry_fees:.2f} | "
			f"edge_score={score:.0f} | {reason}"
		)

		# Record in trades.db
		conn = get_db_connection()
		cur = conn.cursor()
		event_ticker = market.get('event_ticker', '')
		size_dollars = count * contract_price
		cur.execute('''
			INSERT INTO trades (
				timestamp, market_ticker, direction, size, price, pnl, reason,
				status, client_order_id, kalshi_order_id, order_status, fees, event_ticker
			)
			VALUES (datetime('now'), ?, ?, ?, ?, 0.0, ?, 'OPEN', ?, ?, ?, ?, ?)
		''', (
			ticker, direction, size_dollars, contract_price,
			reason,
			client_order_id, kalshi_order_id, order_status, entry_fees, event_ticker,
		))
		conn.commit()
		conn.close()

		# Discord notification
		notify_trade_executed(
			ticker, title, direction, score,
			count, contract_price,
			f"[EDGE score={score:.0f}] orderbook flow",
			total_cost,
			is_undervalued=False,
			order_status=order_status,
			fees=entry_fees,
		)

		try:
			winsound.Beep(1200, 150)
			winsound.Beep(1400, 150)
			winsound.Beep(1600, 150)
			winsound.Beep(1000, 400)
		except Exception:
			pass

		return True

	except Exception as e:
		resp = getattr(e, 'response', None)
		status_code = getattr(resp, 'status_code', 'unknown')
		resp_body = (getattr(resp, 'text', '') or '')[:500]
		logger.error(
			f"[EDGE] Order failed for {ticker}: {e} | status={status_code} | body={resp_body}"
		)
		notify_error(f"[EDGE] Order failed for {ticker}: {e}")
		return False


# ═══════════════════════════════════════════════════════════════════
# Signal logger (signal_log mode)
# ═══════════════════════════════════════════════════════════════════
SIGNAL_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "edge_signals")
os.makedirs(SIGNAL_LOG_DIR, exist_ok=True)
POSITION_SCORE_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "edge_signals", "position_scores")
os.makedirs(POSITION_SCORE_LOG_DIR, exist_ok=True)

def _signal_log_path():
	return os.path.join(SIGNAL_LOG_DIR, f"edge_signals_{datetime.now().strftime('%Y%m%d')}.jsonl")

def log_edge_signal(ticker, direction, score, details, candidate, filtered=False, filter_reasons=None):
	"""Append a structured signal record to the daily JSONL file."""
	market = candidate.get("market", {})
	record = {
		"ts": datetime.now(timezone.utc).isoformat(),
		"ticker": ticker,
		"direction": direction,
		"score": round(score, 1),
		"details": details,
		"yes_price": candidate.get("yes_price"),
		"no_price": candidate.get("no_price"),
		"volume": candidate.get("volume"),
		"close_time": market.get("close_time"),
		"title": (market.get("title") or market.get("event_ticker") or "")[:120],
	}
	if filtered:
		record["filtered"] = True
		record["filter_reasons"] = filter_reasons or []
	with _BOOK_LOCK:
		book = BOOKS.get(ticker, {})
		yes_book = book.get("yes", {})
		no_book = book.get("no", {})
	if yes_book:
		record["yes_best_bid"] = max(yes_book.keys())
		record["yes_total_depth"] = int(sum(yes_book.values()))
	if no_book:
		record["no_best_bid"] = max(no_book.keys())
		record["no_total_depth"] = int(sum(no_book.values()))
	try:
		with open(_signal_log_path(), "a", encoding="utf-8") as f:
			f.write(json.dumps(record) + "\n")
	except Exception as e:
		logger.error(f"[EDGE] Failed to write signal log: {e}")


# ═══════════════════════════════════════════════════════════════════
# Edge scanner stop loss & position score tracking
# ═══════════════════════════════════════════════════════════════════
def get_open_edge_positions():
	"""Return list of actually-open positions entered by edge scanner.

	Uses the Kalshi API to get real open positions, then cross-references
	with trades.db for entry price/direction. This avoids stale DB entries
	where markets have already settled.
	"""
	# 1. Get actual open positions from Kalshi API
	try:
		data = signed_request("GET", "/portfolio/positions")
		api_positions = data.get('market_positions', [])
		live_tickers = {}
		for p in api_positions:
			qty = float(p.get('position_fp', 0) or 0)
			if qty != 0.0:
				live_tickers[p.get('ticker', '')] = qty
	except Exception as e:
		logger.error(f"[EDGE SL] Failed to fetch positions from API: {e}")
		return []

	if not live_tickers:
		return []

	# 2. Cross-reference with trades.db for entry price/direction (edge scanner trades only)
	conn = get_db_connection()
	cur = conn.cursor()
	placeholders = ",".join("?" for _ in live_tickers)
	cur.execute(
		f"SELECT id, market_ticker, direction, size, price, kalshi_order_id "
		f"FROM trades WHERE status = 'OPEN' AND reason LIKE 'edge_scanner%' "
		f"AND market_ticker IN ({placeholders})",
		list(live_tickers.keys()),
	)
	positions = []
	for row in cur.fetchall():
		trade_id, ticker, direction, size, entry_price, kalshi_order_id = row
		contracts = int(round(size / entry_price)) if entry_price > 0 else 0
		positions.append({
			"trade_id": trade_id,
			"ticker": ticker,
			"direction": direction,
			"contracts": contracts,
			"entry_price": entry_price,
			"kalshi_order_id": kalshi_order_id,
		})
	conn.close()
	return positions


def log_position_score_snapshot(ticker, direction, entry_price, current_bid, pos):
	"""Log score + price snapshot for open positions (for future score-based stop loss sim)."""
	try:
		score, details = compute_edge_score(ticker, direction)
		record = {
			"ts": datetime.now(timezone.utc).isoformat(),
			"ticker": ticker,
			"direction": direction,
			"entry_price": entry_price,
			"current_bid": current_bid,
			"pnl_pct": round((current_bid - entry_price) / entry_price * 100, 2) if entry_price > 0 else 0,
			"score": round(score, 1),
			"details": details,
			"trade_id": pos["trade_id"],
		}
		log_path = os.path.join(POSITION_SCORE_LOG_DIR, f"pos_scores_{datetime.now().strftime('%Y%m%d')}.jsonl")
		with open(log_path, "a", encoding="utf-8") as f:
			f.write(json.dumps(record) + "\n")
	except Exception as e:
		logger.debug(f"[EDGE] Failed to log position score for {ticker}: {e}")


def execute_edge_stop_loss(pos, current_bid):
	"""Execute stop loss exit for an edge scanner position."""
	ticker = pos["ticker"]
	direction = pos["direction"]
	contracts = pos["contracts"]
	entry_price = pos["entry_price"]

	side = "yes" if direction == "YES" else "no"
	client_order_id = f"edge-sl-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

	# Price the sell order 1c below current bid for immediate fill
	bid_cents = int(round(current_bid * 100))
	aggressive_price = max(1, bid_cents - 1)

	order_body = {
		"ticker": ticker,
		"action": "sell",
		"side": side,
		"count": contracts,
		"type": "limit",
		"time_in_force": "immediate_or_cancel",
		"post_only": False,
		"client_order_id": client_order_id,
	}
	if side == "yes":
		order_body["yes_price"] = aggressive_price
	else:
		order_body["no_price"] = aggressive_price

	try:
		resp = signed_request("POST", "/portfolio/orders", body=order_body)
		order_data = resp.get('order', resp) if isinstance(resp, dict) else {}
		order_id = order_data.get('order_id') or order_data.get('id')
		order_status = (
			order_data.get('status')
			or (resp.get('status') if isinstance(resp, dict) else None)
		)
		fill_count = float(order_data.get('fill_count_fp', 0) or 0)

		if fill_count <= 0:
			logger.warning(f"[EDGE SL] Stop loss sell for {ticker} NOT filled (status={order_status})")
			# If market is determined/settled, force-close in DB — can't sell but position is lost
			try:
				mkt_data = signed_request("GET", f"/markets/{ticker}")
				mkt = mkt_data.get("market", mkt_data) if isinstance(mkt_data, dict) else {}
				mkt_status = mkt.get("status", "")
				mkt_result = mkt.get("result", "")
				if mkt_status in ("determined", "finalized", "settled") and mkt_result:
					won = (direction == "YES" and mkt_result == "yes") or (direction == "NO" and mkt_result == "no")
					pnl_dollars = (1.0 - entry_price) * contracts if won else -(entry_price * contracts)
					new_status = "WON" if won else "LOST"
					conn = get_db_connection()
					cur = conn.cursor()
					cur.execute(
						"""UPDATE trades SET status = ?, pnl = ?,
						    reason = reason || ' | force_closed_determined',
						    resolved_timestamp = datetime('now')
						WHERE id = ?""",
						(new_status, pnl_dollars, pos["trade_id"]),
					)
					conn.commit()
					conn.close()
					SL_WATCH_TICKERS.discard(ticker)
					logger.warning(
						f"[EDGE SL] Force-closed {ticker} (market {mkt_status}, result={mkt_result}) | "
						f"dir={direction} | status={new_status} | pnl=${pnl_dollars:.2f}"
					)
					return
			except Exception as e:
				logger.debug(f"[EDGE SL] Force-close check failed for {ticker}: {e}")
			return

		# Get actual fill price
		exit_price, exit_fees = (
			fetch_fill_price_and_fees(order_id, side) if order_id else (None, 0.0)
		)
		if exit_price is None:
			exit_price = current_bid
			exit_fees = 0.0

		pnl_per_contract = exit_price - entry_price
		pnl_dollars = pnl_per_contract * contracts - exit_fees

		# Update trades.db
		conn = get_db_connection()
		cur = conn.cursor()
		cur.execute(
			"""UPDATE trades
			SET status = 'CLOSED',
			    pnl = ?,
			    reason = reason || ' | closed by edge_stop_loss',
			    resolved_timestamp = datetime('now')
			WHERE id = ?""",
			(pnl_dollars, pos["trade_id"]),
		)
		conn.commit()
		conn.close()

		SL_WATCH_TICKERS.discard(ticker)

		pnl_pct = (pnl_per_contract / entry_price * 100) if entry_price > 0 else 0

		logger.success(
			f"[EDGE SL] Closed {ticker} | dir={direction} | "
			f"entry=${entry_price:.3f} -> exit=${exit_price:.3f} | "
			f"pnl=${pnl_dollars:.2f} ({pnl_pct:+.1f}%) | fees=${exit_fees:.2f}"
		)

		# Discord notification
		notify_position_closed(
			ticker=ticker,
			direction=direction,
			quantity=contracts,
			entry_price=entry_price,
			exit_price=exit_price,
			pnl_dollars=pnl_dollars,
			pnl_percent=pnl_pct,
			trigger=f"edge_stop_loss ({EDGE_SCANNER_STOP_LOSS_PCT*100:.0f}%)",
			order_status=order_status,
			exit_fees=exit_fees,
		)

		try:
			winsound.Beep(400, 500)  # low tone for stop loss
		except Exception:
			pass

	except Exception as e:
		resp_obj = getattr(e, 'response', None)
		status_code = getattr(resp_obj, 'status_code', 'unknown')
		resp_body = (getattr(resp_obj, 'text', '') or '')[:500]
		logger.error(
			f"[EDGE SL] Stop loss exit failed for {ticker}: {e} | "
			f"status={status_code} | body={resp_body}"
		)

		# 409 Conflict usually means market closed/determined — try force-close
		if status_code == 409:
			try:
				mkt_data = signed_request("GET", f"/markets/{ticker}")
				mkt = mkt_data.get("market", mkt_data) if isinstance(mkt_data, dict) else {}
				mkt_status = mkt.get("status", "")
				mkt_result = mkt.get("result", "")
				if mkt_status in ("determined", "finalized", "settled", "closed") and mkt_result:
					won = (direction == "YES" and mkt_result == "yes") or (direction == "NO" and mkt_result == "no")
					pnl_dollars = (1.0 - entry_price) * contracts if won else -(entry_price * contracts)
					new_status = "WON" if won else "LOST"
					conn = get_db_connection()
					cur = conn.cursor()
					cur.execute(
						"""UPDATE trades SET status = ?, pnl = ?,
						    reason = reason || ' | force_closed_409',
						    resolved_timestamp = datetime('now')
						WHERE id = ?""",
						(new_status, pnl_dollars, pos["trade_id"]),
					)
					conn.commit()
					conn.close()
					SL_WATCH_TICKERS.discard(ticker)
					logger.warning(
						f"[EDGE SL] Force-closed {ticker} after 409 (market {mkt_status}, result={mkt_result}) | "
						f"dir={direction} | status={new_status} | pnl=${pnl_dollars:.2f}"
					)
					return
				elif mkt_status in ("closed",) and not mkt_result:
					logger.warning(f"[EDGE SL] Market {ticker} is closed but no result yet — will retry next cycle")
					return
			except Exception as fc_err:
				logger.debug(f"[EDGE SL] Force-close after 409 failed for {ticker}: {fc_err}")

		notify_error(f"[EDGE SL] Stop loss exit failed for {ticker}: {e}")


def check_edge_stop_losses():
	"""Check open edge positions for stop loss triggers and log score snapshots."""
	positions = get_open_edge_positions()

	# Update SL watch set (keeps WS subscriptions alive for these tickers)
	watched = {p["ticker"] for p in positions}
	SL_WATCH_TICKERS.clear()
	SL_WATCH_TICKERS.update(watched)

	# Ensure open-position tickers are subscribed to WS (they may not be candidates)
	newly_subscribed = set()
	if WS_CLIENT and watched:
		with _BOOK_LOCK:
			unsubscribed = {t for t in watched if t not in BOOKS}
		if unsubscribed:
			loop = getattr(WS_CLIENT, '_loop', None)
			if loop and loop.is_running():
				for t in unsubscribed:
					asyncio.run_coroutine_threadsafe(WS_CLIENT.subscribe_ticker(t), loop)
				newly_subscribed = unsubscribed
				logger.info(f"[EDGE SL] Subscribed {len(unsubscribed)} open-position tickers to WS: {sorted(unsubscribed)}")

	if not positions:
		return

	for pos in positions:
		ticker = pos["ticker"]
		direction = pos["direction"]
		entry_price = pos["entry_price"]

		# Skip tickers just subscribed — book data won't arrive until next cycle
		if ticker in newly_subscribed:
			continue

		# Get current price from WS orderbook
		current_bid = None
		with _BOOK_LOCK:
			book = BOOKS.get(ticker, {})

		if direction == "YES":
			our_book = book.get("yes", {})
		else:
			our_book = book.get("no", {})

		if our_book:
			current_bid = max(our_book.keys())
		else:
			# REST fallback when WS orderbook is empty (common near settlement)
			current_bid = _fetch_rest_bid(ticker, direction)
			if current_bid is not None:
				logger.debug(f"[EDGE SL] Using REST fallback bid for {ticker}: ${current_bid:.3f}")
			else:
				logger.debug(f"[EDGE SL] No orderbook data for {ticker} (WS + REST) — skipping")
				continue

		# Log score snapshot for future score-based stop loss sim (always)
		log_position_score_snapshot(ticker, direction, entry_price, current_bid, pos)

		if not EDGE_SCANNER_STOP_LOSS_ENABLED:
			continue

		# Check price-based stop loss
		pnl_pct = (current_bid - entry_price) / entry_price if entry_price > 0 else 0
		stop_threshold = -EDGE_SCANNER_STOP_LOSS_PCT

		if pnl_pct <= stop_threshold:
			logger.warning(
				f"[EDGE SL] STOP LOSS TRIGGERED: {ticker} | dir={direction} | "
				f"entry=${entry_price:.3f} | current=${current_bid:.3f} | "
				f"pnl={pnl_pct*100:.1f}% <= {stop_threshold*100:.1f}%"
			)
			execute_edge_stop_loss(pos, current_bid)



def _fetch_rest_bid(ticker, direction):
	"""Fetch current bid price via REST API as fallback when WS book is empty.
	Falls back to last_price when bid is 0, and to market result for determined markets."""
	try:
		data = signed_request("GET", f"/markets/{ticker}")
		market = data.get("market", data) if isinstance(data, dict) else {}
		if direction == "YES":
			bid = market.get("yes_bid_dollars") or market.get("yes_bid")
		else:
			bid = market.get("no_bid_dollars") or market.get("no_bid")
		if bid is not None:
			val = float(bid)
			if val > 1.0:
				val /= 100.0
			if val > 0:
				return val

		# Bid is 0 or None — try last_price as proxy (active markets with thin books)
		last_price = float(market.get("last_price_dollars", 0) or 0)
		if last_price > 1.0:
			last_price /= 100.0
		if last_price > 0:
			# last_price is YES-side; for NO direction, invert it
			proxy = last_price if direction == "YES" else (1.0 - last_price)
			if proxy > 0:
				logger.debug(f"[EDGE SL] Using last_price proxy for {ticker} {direction}: ${proxy:.3f}")
				return proxy

		# No price at all — check if market is determined/settled
		status = market.get("status", "")
		result = market.get("result", "")
		if status in ("determined", "finalized", "settled") and result:
			won = (direction == "YES" and result == "yes") or (direction == "NO" and result == "no")
			if won:
				return 1.0
			else:
				logger.info(f"[EDGE SL] Market {ticker} determined as '{result}' — position LOST (dir={direction})")
				return 0.01  # near-zero to trigger stop loss
	except Exception as e:
		logger.debug(f"[EDGE SL] REST bid fetch failed for {ticker}: {e}")
	return None



# ═══════════════════════════════════════════════════════════════════
# Settlement reconciliation
# ═══════════════════════════════════════════════════════════════════
SETTLEMENT_RECONCILE_INTERVAL = 120  # seconds between reconciliation runs
_last_reconcile = 0.0

def reconcile_settled_trades():
	"""Check for settled markets and update stale OPEN trades in the DB."""
	global _last_reconcile
	now = time.time()
	if now - _last_reconcile < SETTLEMENT_RECONCILE_INTERVAL:
		return
	_last_reconcile = now

	# Get all OPEN edge scanner trades from DB
	conn = get_db_connection()
	cur = conn.cursor()
	cur.execute(
		"SELECT id, market_ticker, direction, size, price "
		"FROM trades WHERE status = 'OPEN' AND reason LIKE 'edge_scanner%'"
	)
	open_trades = cur.fetchall()
	if not open_trades:
		conn.close()
		return

	open_by_ticker = {}
	for trade_id, ticker, direction, size, price in open_trades:
		open_by_ticker.setdefault(ticker, []).append({
			"trade_id": trade_id, "direction": direction, "size": size, "price": price,
		})

	# Fetch recent settlements from Kalshi
	try:
		min_ts = int(time.time()) - (7 * 24 * 3600)  # last 7 days
		params = {"limit": 200, "min_ts": min_ts}
		all_settlements = []
		cursor_val = None
		for _ in range(5):  # max 5 pages
			if cursor_val:
				params["cursor"] = cursor_val
			data = signed_request("GET", "/portfolio/settlements", params=params)
			settlements_page = data.get('settlements', [])
			all_settlements.extend(settlements_page)
			cursor_val = data.get('cursor')
			if not cursor_val:
				break
	except Exception as e:
		logger.error(f"[EDGE SETTLE] Failed to fetch settlements: {e}")
		conn.close()
		return

	# Match settlements to open trades
	reconciled = 0
	for settlement in all_settlements:
		ticker = settlement.get('ticker')
		if not ticker or ticker not in open_by_ticker:
			continue

		market_result = settlement.get('market_result', '')
		if isinstance(market_result, str):
			normalized = market_result.strip().lower()
			if normalized in ('yes', 'y'):
				winner = 'YES'
			elif normalized in ('no', 'n'):
				winner = 'NO'
			else:
				continue
		else:
			continue

		settled_time = settlement.get('settled_time', '')
		if isinstance(settled_time, str) and settled_time:
			try:
				if settled_time.endswith('Z'):
					dt = datetime.fromisoformat(settled_time.replace('Z', '+00:00'))
				else:
					dt = datetime.fromisoformat(settled_time)
				if dt.tzinfo is not None:
					dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
				resolved_ts = dt.strftime('%Y-%m-%d %H:%M:%S')
			except ValueError:
				resolved_ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
		else:
			resolved_ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

		for trade in open_by_ticker[ticker]:
			if trade["direction"] == winner:
				status = 'WON'
				pnl = trade["size"]  # size = contracts * entry_price, win = (1.0 - entry) * contracts
				pnl = (1.0 - trade["price"]) * (trade["size"] / trade["price"]) if trade["price"] > 0 else 0
			else:
				status = 'LOST'
				pnl = -trade["size"]

			cur.execute(
				"UPDATE trades SET status = ?, pnl = ?, resolved_timestamp = ? WHERE id = ?",
				(status, pnl, resolved_ts, trade["trade_id"]),
			)
			reconciled += 1
			logger.info(
				f"[EDGE SETTLE] {ticker} → {status} | dir={trade['direction']} | "
				f"entry=${trade['price']:.3f} | pnl=${pnl:.2f}"
			)

	if reconciled > 0:
		conn.commit()
		logger.info(f"[EDGE SETTLE] Reconciled {reconciled} settled trades")
	conn.close()


# ═══════════════════════════════════════════════════════════════════
# Evaluation & main loop
# ═══════════════════════════════════════════════════════════════════
def evaluate_candidates():
	"""Score all candidates; attempt entry or log signal depending on mode."""
	with _STATE_LOCK:
		snapshot = dict(CANDIDATES)
	now_mono = time.monotonic()

	# Fetch recently SL-exited tickers to prevent re-entry (cross-process via DB)
	try:
		sl_cooldown_tickers = get_recent_sl_exits(minutes=5)
	except Exception:
		sl_cooldown_tickers = set()

	for ticker, candidate in snapshot.items():
		# Cooldown check
		last = LAST_ATTEMPT.get(ticker, 0)
		if now_mono - last < EDGE_SCANNER_COOLDOWN_SECONDS:
			continue

		# SL re-entry cooldown: skip tickers that were stopped out recently
		if ticker in sl_cooldown_tickers:
			logger.debug(f"[EDGE] SKIP {ticker}: recent SL exit cooldown (5 min)")
			continue

		direction = candidate["direction"]

		if EDGE_SCANNER_MODE == "signal_log":
			# Score both sides, pick the stronger one
			yes_score, yes_details = compute_edge_score(ticker, "YES")
			no_score, no_details = compute_edge_score(ticker, "NO")
			if yes_score >= no_score:
				score, details, direction = yes_score, yes_details, "YES"
			else:
				score, details, direction = no_score, no_details, "NO"

			if score < EDGE_SCANNER_MIN_SCORE:
				continue

			# --- Entry price & imbalance filters (log with tag, don't suppress) ---
			entry_price = details.get("best_bid", 0)
			imbalance_pts = details.get("imbalance_pts", 0)
			filter_reasons = []
			if entry_price < EDGE_SCANNER_ENTRY_MIN:
				filter_reasons.append(f"entry_low_{entry_price:.2f}<{EDGE_SCANNER_ENTRY_MIN}")
			if entry_price > EDGE_SCANNER_ENTRY_MAX:
				filter_reasons.append(f"entry_high_{entry_price:.2f}>{EDGE_SCANNER_ENTRY_MAX}")
			if imbalance_pts < EDGE_SCANNER_MIN_IMBALANCE_PTS:
				filter_reasons.append(f"imbal_low_{imbalance_pts:.0f}<{EDGE_SCANNER_MIN_IMBALANCE_PTS:.0f}")
			is_filtered = len(filter_reasons) > 0

			tag = "[FILTERED]" if is_filtered else "[LOG ONLY]"
			logger.info(
				f"[EDGE] SIGNAL: {ticker} | dir={direction} | score={score:.0f}/{EDGE_SCANNER_MIN_SCORE} | "
				f"imbalance={details.get('imbalance', 0):.2f}({details.get('imbalance_pts', 0):.0f}) | "
				f"spread={details.get('spread_cents', 0)}¢({details.get('spread_pts', 0):.0f}) | "
				f"top={details.get('best_bid_size', 0)}({details.get('top_pts', 0):.0f}) | "
				f"flow={details.get('flow_30s', 0)}({details.get('flow_pts', 0):.0f}) | "
				f"entry=${entry_price:.3f} | {tag}"
			)
			log_edge_signal(ticker, direction, score, details, candidate,
			                filtered=is_filtered, filter_reasons=filter_reasons)

			with _STATE_LOCK:
				LAST_ATTEMPT[ticker] = now_mono

		else:
			# Live mode: score both sides, pick the stronger one, then filter + execute
			yes_score, yes_details = compute_edge_score(ticker, "YES")
			no_score, no_details = compute_edge_score(ticker, "NO")
			if yes_score >= no_score:
				score, details, direction = yes_score, yes_details, "YES"
			else:
				score, details, direction = no_score, no_details, "NO"

			if score < EDGE_SCANNER_MIN_SCORE:
				continue

			# --- Always log the signal (for ongoing analysis) ---
			entry_price = details.get("best_bid", 0)
			imbalance_pts = details.get("imbalance_pts", 0)
			filter_reasons = []
			if entry_price < EDGE_SCANNER_ENTRY_MIN:
				filter_reasons.append(f"entry_low_{entry_price:.2f}<{EDGE_SCANNER_ENTRY_MIN}")
			if entry_price > EDGE_SCANNER_ENTRY_MAX:
				filter_reasons.append(f"entry_high_{entry_price:.2f}>{EDGE_SCANNER_ENTRY_MAX}")
			if imbalance_pts < EDGE_SCANNER_MIN_IMBALANCE_PTS:
				filter_reasons.append(f"imbal_low_{imbalance_pts:.0f}<{EDGE_SCANNER_MIN_IMBALANCE_PTS:.0f}")
			max_profit_per_contract = 1.0 - entry_price
			if max_profit_per_contract <= 0.05:
				filter_reasons.append(f"fee_unprofitable_{max_profit_per_contract:.3f}<=0.05")
			is_filtered = len(filter_reasons) > 0

			tag = "[FILTERED]" if is_filtered else "[LIVE]"
			logger.info(
				f"[EDGE] SIGNAL: {ticker} | dir={direction} | score={score:.0f}/{EDGE_SCANNER_MIN_SCORE} | "
				f"imbalance={details.get('imbalance', 0):.2f}({details.get('imbalance_pts', 0):.0f}) | "
				f"spread={details.get('spread_cents', 0)}¢({details.get('spread_pts', 0):.0f}) | "
				f"top={details.get('best_bid_size', 0)}({details.get('top_pts', 0):.0f}) | "
				f"flow={details.get('flow_30s', 0)}({details.get('flow_pts', 0):.0f}) | "
				f"entry=${entry_price:.3f} | {tag}"
			)
			log_edge_signal(ticker, direction, score, details, candidate,
			                filtered=is_filtered, filter_reasons=filter_reasons)

			with _STATE_LOCK:
				LAST_ATTEMPT[ticker] = now_mono

			# --- Skip execution for filtered signals ---
			if is_filtered:
				continue

			success = attempt_entry(ticker, direction, candidate, score, details)
			if success:
				with _STATE_LOCK:
					CANDIDATES.pop(ticker, None)
				break  # one trade per eval cycle


def scanner_loop():
	"""Run the edge scanner continuously."""
	last_refresh = 0.0
	logger.info(
		f"[EDGE] Starting scanner | mode={EDGE_SCANNER_MODE} | min_score={EDGE_SCANNER_MIN_SCORE} | "
		f"cooldown={EDGE_SCANNER_COOLDOWN_SECONDS}s | eval_interval={SCANNER_EVAL_INTERVAL}s | "
		f"market_refresh={MARKET_REFRESH_INTERVAL}s | "
		f"entry=${EDGE_SCANNER_ENTRY_MIN}-${EDGE_SCANNER_ENTRY_MAX} | "
		f"min_imbalance={EDGE_SCANNER_MIN_IMBALANCE_PTS}pts | "
		f"stop_loss={'ON '+str(int(EDGE_SCANNER_STOP_LOSS_PCT*100))+'%' if EDGE_SCANNER_STOP_LOSS_ENABLED else 'OFF'}"
	)
	if EDGE_SCANNER_MODE == "signal_log":
		logger.info(f"[EDGE] Signal-log mode: logging to {SIGNAL_LOG_DIR}/ — NO trades will be placed")

	while True:
		try:
			if discord_bot.is_paused("execution"):
				logger.info("[EDGE] Paused via Discord — sleeping")
				time.sleep(SCANNER_EVAL_INTERVAL)
				continue

			now = time.time()
			if now - last_refresh >= MARKET_REFRESH_INTERVAL:
				try:
					if EDGE_SCANNER_MODE == "signal_log":
						# No balance check needed in signal_log mode
						refresh_markets()
					else:
						bal_data = signed_request("GET", "/portfolio/balance")
						cash = bal_data.get('balance', 0) / 100
						if cash >= EDGE_SCANNER_RISK_FLAT:
							refresh_markets()
						else:
							logger.warning(
								f"[EDGE] Cash ${cash:.2f} < ${EDGE_SCANNER_RISK_FLAT:.2f} — skipping refresh"
							)
				except Exception as e:
					logger.error(f"[EDGE] Market refresh error: {e}")
				last_refresh = now

			# Daily limit check (live mode only)
			if EDGE_SCANNER_MODE != "signal_log" and get_daily_trade_count() >= MAX_TRADES_PER_DAY:
				time.sleep(60)
				continue

			# Check stop losses on open positions + log score snapshots
			if EDGE_SCANNER_MODE != "signal_log":
				try:
					check_edge_stop_losses()
				except Exception as e:
					logger.error(f"[EDGE SL] Stop loss check error: {e}")

			# Reconcile settled trades in DB (every 2 min)
			if EDGE_SCANNER_MODE != "signal_log":
				try:
					reconcile_settled_trades()
				except Exception as e:
					logger.error(f"[EDGE SETTLE] Reconciliation error: {e}")

			evaluate_candidates()

		except Exception as e:
			logger.error(f"[EDGE] Scanner loop error: {e}")

		time.sleep(SCANNER_EVAL_INTERVAL)


# ═══════════════════════════════════════════════════════════════════
# WS client management
# ═══════════════════════════════════════════════════════════════════
def start_ws_client(tickers):
	global WS_CLIENT
	WS_CLIENT = KalshiWebSocketClient(
		KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, set(tickers), handle_orderbook
	)

	def run():
		asyncio.run(WS_CLIENT.connect())

	t = threading.Thread(target=run, daemon=True)
	t.start()
	logger.info(f"[EDGE] Started WS client | initial tickers: {len(tickers)}")


# ═══════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
	if not EDGE_SCANNER_ENABLED:
		logger.error(
			"[EDGE] EDGE_SCANNER_ENABLED is False — exiting. "
			"Set EDGE_SCANNER_ENABLED=true in .env to enable."
		)
		exit(1)

	# Start Discord command listener
	discord_bot.start_command_listener(respond=False)

	# Initial market scan and WS setup
	logger.info("[EDGE] Performing initial market scan...")
	try:
		markets = fetch_markets_rest()
		initial_candidates = build_candidates(markets)
		with _STATE_LOCK:
			CANDIDATES.update(initial_candidates)
		tickers = list(initial_candidates.keys())
		logger.info(f"[EDGE] Initial candidates: {len(tickers)} — {sorted(tickers)}")
		start_ws_client(tickers)
		time.sleep(2)  # brief wait for initial WS snapshots
	except Exception as e:
		logger.error(f"[EDGE] Initial scan failed: {e}")
		start_ws_client([])

	scanner_loop()
