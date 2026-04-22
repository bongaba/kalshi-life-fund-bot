"""
Edge scanner ROI analysis — $1000 bankroll, evenly split across all trades.
Uses signal_log data + Kalshi API settlement results.
Outputs to edge_roi_analysis.txt in the same format as edge_roi_clean.txt.
"""
import json, os, sys, time, base64, math
from datetime import datetime, timezone
from collections import defaultdict
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import MODE, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

host = "https://demo-api.kalshi.co" if MODE == "demo" else "https://api.elections.kalshi.com"
api_prefix = "/trade-api/v2"

with open(KALSHI_PRIVATE_KEY_PATH, "rb") as f:
    private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def signed_request(method, path, params=None):
    timestamp = str(int(time.time() * 1000))
    full_path = api_prefix + path
    sign_path = full_path.split('?')[0]
    message = f"{timestamp}{method}{sign_path}".encode('utf-8')
    sig = private_key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    headers = {"KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID, "KALSHI-ACCESS-TIMESTAMP": timestamp, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}
    url = host + full_path
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    return resp

def fetch_market(ticker):
    for attempt in range(3):
        try:
            resp = signed_request("GET", f"/markets/{ticker}")
            if resp.status_code == 200:
                return resp.json().get("market", {})
            return None
        except (Exception, KeyboardInterrupt) as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"  WARN: failed to fetch {ticker} after 3 attempts: {e}")
                return None

# ── Disk cache for market data to avoid re-fetching ──
MARKET_CACHE_FILE = "logs/edge_signals/_market_cache.json"

def load_market_cache():
    if os.path.exists(MARKET_CACHE_FILE):
        try:
            with open(MARKET_CACHE_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}

def save_market_cache(cache):
    os.makedirs(os.path.dirname(MARKET_CACHE_FILE), exist_ok=True)
    with open(MARKET_CACHE_FILE, "w") as f:
        json.dump(cache, f)

FEE_PER_CONTRACT = 0.02
BANKROLL = 1000.00

# ── Load signals ──
signals = []
for fname in sorted(os.listdir("logs/edge_signals")):
    if not fname.endswith(".jsonl"):
        continue
    with open(os.path.join("logs/edge_signals", fname), "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                signals.append(json.loads(line))
            except:
                continue

print(f"Total signals loaded: {len(signals)}", flush=True)

# Deduplicate: keep last signal per ticker
by_ticker = {}
for s in signals:
    tk = s["ticker"]
    if tk not in by_ticker or s["ts"] > by_ticker[tk]["ts"]:
        by_ticker[tk] = s

filtered_tickers = {tk: s for tk, s in by_ticker.items() if s.get("filtered")}
unfiltered_tickers = {tk: s for tk, s in by_ticker.items() if not s.get("filtered")}
print(f"Unique tickers: {len(by_ticker)}", flush=True)
print(f"Would-trade (unfiltered): {len(unfiltered_tickers)}", flush=True)
print(f"Filtered out: {len(filtered_tickers)}", flush=True)

# ── Fetch settlements ──
print(f"Fetching settlement data for {len(by_ticker)} markets...", flush=True)
cache = load_market_cache()
fetched = 0
fetched_new = 0
total_to_fetch = len(by_ticker)

def get_settlement(ticker):
    global fetched, fetched_new
    if ticker in cache:
        fetched += 1
        if fetched % 20 == 0:
            print(f"  Fetched {fetched}/{total_to_fetch} (cached)...", flush=True)
        return cache[ticker]
    try:
        mkt = fetch_market(ticker)
    except (Exception, KeyboardInterrupt) as e:
        print(f"  ERROR fetching {ticker}: {e}", flush=True)
        return None
    fetched += 1
    fetched_new += 1
    if fetched % 20 == 0:
        print(f"  Fetched {fetched}/{total_to_fetch}...", flush=True)
    time.sleep(0.12)
    if mkt:
        cache[ticker] = mkt
        if fetched_new % 10 == 0:
            save_market_cache(cache)
    return mkt

# Build trade records with settlement outcome
all_trades = []
for tk, sig in sorted(by_ticker.items(), key=lambda x: x[1]["ts"]):
    mkt = get_settlement(tk)
    if not mkt:
        continue
    status = mkt.get("status", "")
    result = mkt.get("result", "")
    if status not in ("finalized", "settled"):
        continue  # skip pending

    d = sig["direction"]
    entry = sig.get("yes_price", 0) if d == "YES" else sig.get("no_price", 0)
    won = (d == "YES" and result == "yes") or (d == "NO" and result == "no")
    is_filtered = sig.get("filtered", False)
    score = sig.get("score", 0)

    # Use settlement_ts (actual settle) or close_time as fallback for when capital returns
    settle_time = mkt.get("settlement_ts") or mkt.get("close_time") or ""

    all_trades.append({
        "ticker": tk,
        "direction": d,
        "score": score,
        "entry": entry,
        "won": won,
        "filtered": is_filtered,
        "filter_reasons": sig.get("filter_reasons", []),
        "ts": sig["ts"],
        "settle_time": settle_time,
    })

print(f"Settled trades: {len(all_trades)} ({sum(1 for t in all_trades if not t['filtered'])} unfiltered, {sum(1 for t in all_trades if t['filtered'])} filtered)", flush=True)
if fetched_new > 0:
    save_market_cache(cache)
    print(f"  Saved {len(cache)} markets to disk cache.", flush=True)

# ── ROI calculation helpers ──

def calc_strategy(trades, bankroll):
    """Given a list of trade dicts, calculate ROI with even allocation."""
    n = len(trades)
    if n == 0:
        return None
    per_trade = bankroll / n
    rows = []
    total_gross = 0.0
    total_fees = 0.0
    wins = 0
    losses = 0
    for t in sorted(trades, key=lambda x: x["ts"]):
        entry = t["entry"]
        if entry <= 0:
            continue
        contracts = math.floor(per_trade / entry)
        if contracts <= 0:
            continue
        fees = contracts * FEE_PER_CONTRACT
        if t["won"]:
            gross = contracts * (1.0 - entry)
            wl = "WIN"
            wins += 1
        else:
            gross = -(contracts * entry)
            wl = "LOSS"
            losses += 1
        net = gross - fees
        total_gross += gross
        total_fees += fees
        rows.append({
            "ticker": t["ticker"],
            "direction": t["direction"],
            "score": t["score"],
            "entry": entry,
            "contracts": contracts,
            "wl": wl,
            "gross": gross,
            "fees": fees,
            "net": net,
        })
    total_net = total_gross - total_fees
    roi = (total_net / bankroll) * 100
    return {
        "rows": rows,
        "total_gross": total_gross,
        "total_fees": total_fees,
        "total_net": total_net,
        "roi": roi,
        "wins": wins,
        "losses": losses,
        "per_trade": per_trade,
        "n": n,
    }


def format_strategy(title, subtitle, bankroll, trades):
    """Format a strategy block as text."""
    result = calc_strategy(trades, bankroll)
    if result is None:
        return f"\n{'='*100}\n  {title}\n  {subtitle}\n{'='*100}\n  No trades.\n"

    lines = []
    lines.append(f"\n{'='*100}")
    lines.append(f"  {title}")
    lines.append(f"  {subtitle}")
    lines.append(f"  Bankroll: ${bankroll:,.0f} | Trades: {result['n']} | Per trade: ${result['per_trade']:.2f}")
    lines.append(f"{'='*100}")
    lines.append(f"  {'Ticker':<45s} Dir  Scr  Entry  Ctrs  W/L     Gross   Fees       Net")
    lines.append(f"  {'-'*95}")
    for r in result["rows"]:
        lines.append(
            f"  {r['ticker']:<45s} {r['direction']:>3s} {r['score']:5.0f} ${r['entry']:.3f} {r['contracts']:5d}  "
            f"{r['wl']:<4s} ${r['gross']:+9.2f} ${r['fees']:5.2f} ${r['net']:+9.2f}"
        )
    lines.append(f"  {'-'*95}")
    lines.append(f"  GROSS PROFIT: ${result['total_gross']:+12.2f}")
    lines.append(f"  TOTAL FEES:   ${result['total_fees']:12.2f}")
    lines.append(f"  NET PROFIT:   ${result['total_net']:+12.2f}")
    lines.append(f"  NET ROI:      {result['roi']:+.1f}%")
    total = result['wins'] + result['losses']
    wr = result['wins'] / total * 100 if total else 0
    lines.append(f"  Record:       {result['wins']}W / {result['losses']}L ({wr:.0f}% win rate)")
    return "\n".join(lines)


# ── Build strategies ──
unfiltered_settled = [t for t in all_trades if not t["filtered"]]
filtered_settled = [t for t in all_trades if t["filtered"]]
all_settled = all_trades  # includes both

output_blocks = []

# Header
output_blocks.append("=" * 100)
output_blocks.append("  EDGE SCANNER ROI ANALYSIS — Signal Log Evaluation")
output_blocks.append(f"  Data: {len(signals)} total signals, {len(by_ticker)} unique tickers")
output_blocks.append(f"  Date range: {min(s['ts'] for s in signals)[:10]} to {max(s['ts'] for s in signals)[:10]}")
output_blocks.append(f"  Bankroll: $1,000")
output_blocks.append("=" * 100)

# Strategy 1: All unfiltered (would-trade) signals
output_blocks.append(format_strategy(
    "STRATEGY 1: ALL UNFILTERED SIGNALS (would-trade, score >= 60 + entry/imbalance filters passed)",
    "These are the trades the scanner would have executed in live mode.",
    BANKROLL, unfiltered_settled))

# Strategy 2: Unfiltered, score >= 70
output_blocks.append(format_strategy(
    "STRATEGY 2: UNFILTERED, SCORE >= 70",
    "Higher confidence subset.",
    BANKROLL, [t for t in unfiltered_settled if t["score"] >= 70]))

# Strategy 3: Unfiltered, score >= 80
output_blocks.append(format_strategy(
    "STRATEGY 3: UNFILTERED, SCORE >= 80",
    "Highest confidence subset.",
    BANKROLL, [t for t in unfiltered_settled if t["score"] >= 80]))

# Strategy 4: Unfiltered, entry 0.30-0.70 only (mid-range sweet spot)
output_blocks.append(format_strategy(
    "STRATEGY 4: UNFILTERED, ENTRY $0.30–$0.70 ONLY",
    "Mid-range price sweet spot (highest PnL per trade from eval).",
    BANKROLL, [t for t in unfiltered_settled if 0.30 <= t["entry"] <= 0.70]))

# Strategy 5: ALL signals (unfiltered + filtered) — what if we removed all filters
output_blocks.append(format_strategy(
    "STRATEGY 5: ALL SIGNALS (unfiltered + filtered, no filters applied)",
    "What if we traded every signal the scanner produced, regardless of filters.",
    BANKROLL, all_settled))

# Strategy 6: All signals, score >= 70
output_blocks.append(format_strategy(
    "STRATEGY 6: ALL SIGNALS, SCORE >= 70",
    "Every signal with score >= 70, ignoring entry/imbalance filters.",
    BANKROLL, [t for t in all_settled if t["score"] >= 70]))

# Strategy 7: All signals, entry 0.30-0.85 + imbalance >= 10 (current filter set, applied to all)
output_blocks.append(format_strategy(
    "STRATEGY 7: ALL SIGNALS WITH CURRENT FILTERS RE-APPLIED",
    "Apply entry $0.30-$0.85 + imbalance >= 10 to every signal (sanity check = should match Strategy 1).",
    BANKROLL, [t for t in all_settled 
               if 0.30 <= t["entry"] <= 0.85 
               and not any("imbal_low" in r for r in t.get("filter_reasons", []))
               and not t["filtered"]]))

# Strategy 8: Filtered only (what we rejected)
output_blocks.append(format_strategy(
    "STRATEGY 8: FILTERED-ONLY SIGNALS (what we rejected)",
    "Trades the filters blocked — were they rightfully rejected?",
    BANKROLL, filtered_settled))

# Strategy 9: All signals, score >= 70, entry $0.30-$0.70 (optimal combo)
output_blocks.append(format_strategy(
    "STRATEGY 9: ALL SIGNALS, SCORE >= 70, ENTRY $0.30–$0.70 (OPTIMAL COMBO)",
    "S6's high-confidence scoring + S4's sensible risk/reward filtering.",
    BANKROLL, [t for t in all_settled if t["score"] >= 70 and 0.30 <= t["entry"] <= 0.70]))

# ── Sequential (realistic live) simulation with capital locking ──
def sequential_sim(trades, bankroll, risk_per_trade=100.0, max_trades_per_day=None, slippage_cents=1, fill_rate=1.0):
    """
    Simulate live trading with capital locking:
    - Trades are processed chronologically by entry time (ts).
    - Flat $risk_per_trade allocated per trade from available cash.
    - Capital stays locked until the event settles (settle_time).
    - On settlement, cost + P&L returns to available cash.
    - If available cash < risk_per_trade, trade is skipped (wait for settlements).
    - max_trades_per_day: cap trades per calendar day (None = unlimited).
    - slippage_cents: entry price worsened by N cents (IOC slippage).
    - fill_rate: fraction of trades that actually fill (0.0-1.0).
    """
    import random
    from datetime import datetime, timezone

    def parse_ts(s):
        s = s.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except:
            return datetime.min.replace(tzinfo=timezone.utc)

    sorted_trades = sorted(trades, key=lambda x: x["ts"])
    if not sorted_trades:
        return None

    available = bankroll       # cash on hand
    locked = 0.0               # capital currently in open positions
    peak_total = bankroll
    max_dd = 0.0
    wins = 0
    losses = 0
    total_fees = 0.0
    rows = []
    open_positions = []        # list of (settle_datetime, cost, pnl)

    trades_today = 0
    current_day = None
    random.seed(42)  # deterministic fill simulation

    for t in sorted_trades:
        entry_time = parse_ts(t["ts"])
        settle_time_str = t.get("settle_time", "")
        settle_dt = parse_ts(settle_time_str) if settle_time_str else entry_time

        # ── Daily trade cap ──
        trade_day = entry_time.date()
        if trade_day != current_day:
            current_day = trade_day
            trades_today = 0

        # ── Free up capital from positions that settled before this trade's entry ──
        still_open = []
        for pos in open_positions:
            if pos["settle_dt"] <= entry_time:
                # Position settled — return cost + P&L to available
                available += pos["cost"] + pos["pnl"]
                locked -= pos["cost"]
            else:
                still_open.append(pos)
        open_positions = still_open

        entry_price = t["entry"]
        if entry_price <= 0:
            continue

        # ── Skip if daily cap reached ──
        if max_trades_per_day is not None and trades_today >= max_trades_per_day:
            total_balance = available + locked
            rows.append({
                "ticker": t["ticker"], "direction": t["direction"],
                "score": t["score"], "entry": entry_price, "contracts": 0,
                "wl": "SKIP", "gross": 0, "fees": 0, "net": 0,
                "available": available, "locked": locked,
                "balance": total_balance, "ts": t["ts"],
            })
            continue

        # ── Fill rate simulation — random no-fill ──
        if fill_rate < 1.0 and random.random() > fill_rate:
            total_balance = available + locked
            rows.append({
                "ticker": t["ticker"], "direction": t["direction"],
                "score": t["score"], "entry": entry_price, "contracts": 0,
                "wl": "SKIP", "gross": 0, "fees": 0, "net": 0,
                "available": available, "locked": locked,
                "balance": total_balance, "ts": t["ts"],
            })
            continue

        # ── Apply slippage — worse entry price ──
        entry_price = min(entry_price + slippage_cents / 100.0, 0.99)

        alloc = min(risk_per_trade, available)
        contracts = math.floor(alloc / entry_price)
        if contracts <= 0:
            total_balance = available + locked
            rows.append({
                "ticker": t["ticker"], "direction": t["direction"],
                "score": t["score"], "entry": entry_price, "contracts": 0,
                "wl": "SKIP", "gross": 0, "fees": 0, "net": 0,
                "available": available, "locked": locked,
                "balance": total_balance, "ts": t["ts"],
            })
            continue

        cost = contracts * entry_price  # capital locked for this trade
        fees = contracts * FEE_PER_CONTRACT
        trades_today += 1

        if t["won"]:
            gross = contracts * (1.0 - entry_price)
            wl = "WIN"
            wins += 1
        else:
            gross = -(contracts * entry_price)
            wl = "LOSS"
            losses += 1

        net = gross - fees
        total_fees += fees

        # Lock the cost from available cash
        available -= cost
        locked += cost

        # Record the open position — will settle at settle_dt
        pnl = net  # what we get back on top of cost (can be negative)
        open_positions.append({
            "settle_dt": settle_dt,
            "cost": cost,
            "pnl": pnl,
        })

        total_balance = available + locked  # note: locked includes unrealized P&L via cost only
        # For accurate balance, add pending P&L
        pending_pnl = sum(p["pnl"] for p in open_positions)
        true_balance = available + locked + pending_pnl

        if true_balance > peak_total:
            peak_total = true_balance
        dd = (peak_total - true_balance) / peak_total * 100 if peak_total > 0 else 0
        if dd > max_dd:
            max_dd = dd

        rows.append({
            "ticker": t["ticker"], "direction": t["direction"],
            "score": t["score"], "entry": entry_price, "contracts": contracts,
            "wl": wl, "gross": gross, "fees": fees, "net": net,
            "available": available, "locked": locked,
            "balance": true_balance, "ts": t["ts"],
        })

    # ── Settle all remaining open positions ──
    for pos in open_positions:
        available += pos["cost"] + pos["pnl"]
        locked -= pos["cost"]
    open_positions = []

    final_balance = available
    total_net = final_balance - bankroll
    roi = (total_net / bankroll) * 100

    return {
        "rows": rows,
        "balance": final_balance,
        "total_net": total_net,
        "total_fees": total_fees,
        "roi": roi,
        "wins": wins,
        "losses": losses,
        "max_dd": max_dd,
        "peak": peak_total,
        "n": len([r for r in rows if r["wl"] != "SKIP"]),
        "skipped": len([r for r in rows if r["wl"] == "SKIP"]),
    }


def format_sequential(title, subtitle, bankroll, trades, risk_per_trade=100.0, max_trades_per_day=None, slippage_cents=1, fill_rate=1.0):
    """Format a sequential simulation block."""
    result = sequential_sim(trades, bankroll, risk_per_trade, max_trades_per_day, slippage_cents, fill_rate)
    if result is None:
        return f"\n{'='*100}\n  {title}\n  {subtitle}\n{'='*100}\n  No trades.\n"
    lines = []
    lines.append(f"\n{'='*100}")
    lines.append(f"  {title}")
    lines.append(f"  {subtitle}")
    lines.append(f"  Start: ${bankroll:,.0f} | ${risk_per_trade:.0f} per trade | Trades: {result['n']} (skipped: {result['skipped']})")
    lines.append(f"{'='*100}")
    lines.append(f"  {'Ticker':<45s} Dir  Scr  Entry  Ctrs  W/L     Gross   Fees       Net    Avail     Locked   Balance")
    lines.append(f"  {'-'*130}")
    for r in result["rows"]:
        if r["wl"] == "SKIP":
            lines.append(
                f"  {r['ticker']:<45s} {r['direction']:>3s} {r['score']:5.0f} ${r['entry']:.3f}     -  "
                f"SKIP        -       -         -  ${r['available']:>9.2f} ${r['locked']:>9.2f} ${r['balance']:>9.2f}"
            )
        else:
            lines.append(
                f"  {r['ticker']:<45s} {r['direction']:>3s} {r['score']:5.0f} ${r['entry']:.3f} {r['contracts']:5d}  "
                f"{r['wl']:<4s} ${r['gross']:+9.2f} ${r['fees']:5.2f} ${r['net']:+9.2f} ${r['available']:>9.2f} ${r['locked']:>9.2f} ${r['balance']:>9.2f}"
            )
    lines.append(f"  {'-'*130}")
    lines.append(f"  STARTING BALANCE: ${bankroll:>12,.2f}")
    lines.append(f"  ENDING BALANCE:   ${result['balance']:>12,.2f}")
    lines.append(f"  NET PROFIT:       ${result['total_net']:>+12,.2f}")
    lines.append(f"  TOTAL FEES:       ${result['total_fees']:>12,.2f}")
    lines.append(f"  NET ROI:          {result['roi']:>+.1f}%")
    lines.append(f"  MAX DRAWDOWN:     {result['max_dd']:.1f}%")
    lines.append(f"  PEAK BALANCE:     ${result['peak']:>12,.2f}")
    total = result['wins'] + result['losses']
    wr = result['wins'] / total * 100 if total else 0
    lines.append(f"  Record:           {result['wins']}W / {result['losses']}L ({wr:.0f}% win rate)")
    return "\n".join(lines)


def sequential_daily_breakdown(trades, label, bankroll=BANKROLL, risk_per_trade=100.0):
    """Sequential sim with capital locking, flat $ per trade, daily grouping."""
    from datetime import datetime, timezone

    def parse_ts(s):
        s = s.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except:
            return datetime.min.replace(tzinfo=timezone.utc)

    sorted_trades = sorted(trades, key=lambda x: x["ts"])
    if not sorted_trades:
        return f"\n  {label}: No trades.\n"

    available = bankroll
    locked = 0.0
    open_positions = []
    by_date = defaultdict(lambda: {"wins": 0, "losses": 0, "net": 0.0, "n": 0, "skipped": 0})

    for t in sorted_trades:
        entry_time = parse_ts(t["ts"])
        settle_time_str = t.get("settle_time", "")
        settle_dt = parse_ts(settle_time_str) if settle_time_str else entry_time
        day = t["ts"][:10]

        # Free settled positions
        still_open = []
        for pos in open_positions:
            if pos["settle_dt"] <= entry_time:
                available += pos["cost"] + pos["pnl"]
                locked -= pos["cost"]
            else:
                still_open.append(pos)
        open_positions = still_open

        entry_price = t["entry"]
        if entry_price <= 0:
            continue

        alloc = min(risk_per_trade, available)
        contracts = math.floor(alloc / entry_price)
        if contracts <= 0:
            by_date[day]["skipped"] += 1
            continue

        cost = contracts * entry_price
        fees = contracts * FEE_PER_CONTRACT
        if t["won"]:
            gross = contracts * (1.0 - entry_price)
            by_date[day]["wins"] += 1
        else:
            gross = -(contracts * entry_price)
            by_date[day]["losses"] += 1
        net = gross - fees
        available -= cost
        locked += cost
        open_positions.append({"settle_dt": settle_dt, "cost": cost, "pnl": net})
        by_date[day]["net"] += net
        by_date[day]["n"] += 1

    # Settle remaining
    for pos in open_positions:
        available += pos["cost"] + pos["pnl"]
        locked -= pos["cost"]

    final_balance = available
    lines = []
    cum_net = 0.0
    cum_wins = 0
    cum_losses = 0
    for day in sorted(by_date.keys()):
        d = by_date[day]
        cum_net += d["net"]
        cum_wins += d["wins"]
        cum_losses += d["losses"]
        day_roi = (d["net"] / bankroll) * 100
        cum_roi = (cum_net / bankroll) * 100
        total = d["wins"] + d["losses"]
        wr = d["wins"] / total * 100 if total else 0
        cum_total = cum_wins + cum_losses
        cum_wr = cum_wins / cum_total * 100 if cum_total else 0
        skip_note = f" ({d['skipped']} skip)" if d["skipped"] else ""
        lines.append(
            f"  {day}  |  {d['n']:3d} trades{skip_note:<10s} |  "
            f"{d['wins']:2d}W/{d['losses']:2d}L ({wr:3.0f}%)  |  "
            f"Day: ${d['net']:+8.2f} ({day_roi:+6.1f}%)  |  "
            f"Cumul: ${cum_net:+9.2f} ({cum_roi:+6.1f}%)  {cum_wins}W/{cum_losses}L ({cum_wr:.0f}%)"
        )
    lines.append(f"  {'─'*130}")
    lines.append(f"  Final balance: ${final_balance:,.2f} (started ${bankroll:,.0f}) → ROI: {((final_balance-bankroll)/bankroll)*100:+.1f}%")
    return "\n".join(lines)


# ── Daily ROI breakdown ──
def daily_breakdown(trades, label):
    """Group trades by date and compute ROI per day + cumulative."""
    from collections import OrderedDict
    by_date = defaultdict(list)
    for t in trades:
        day = t["ts"][:10]  # YYYY-MM-DD
        by_date[day].append(t)
    
    if not by_date:
        return f"\n  {label}: No trades.\n"
    
    lines = []
    cum_net = 0.0
    cum_gross = 0.0
    cum_fees = 0.0
    cum_wins = 0
    cum_losses = 0
    
    for day in sorted(by_date.keys()):
        day_trades = by_date[day]
        result = calc_strategy(day_trades, BANKROLL)
        if result is None:
            continue
        cum_net += result["total_net"]
        cum_gross += result["total_gross"]
        cum_fees += result["total_fees"]
        cum_wins += result["wins"]
        cum_losses += result["losses"]
        total = result["wins"] + result["losses"]
        wr = result["wins"] / total * 100 if total else 0
        cum_total = cum_wins + cum_losses
        cum_wr = cum_wins / cum_total * 100 if cum_total else 0
        cum_roi = (cum_net / BANKROLL) * 100
        lines.append(
            f"  {day}  |  {result['n']:3d} trades  |  "
            f"{result['wins']:2d}W/{result['losses']:2d}L ({wr:3.0f}%)  |  "
            f"Day: ${result['total_net']:+8.2f} ({result['roi']:+6.1f}%)  |  "
            f"Cumul: ${cum_net:+9.2f} ({cum_roi:+6.1f}%)  {cum_wins}W/{cum_losses}L ({cum_wr:.0f}%)"
        )
    return "\n".join(lines)

output_blocks.append(f"\n{'='*100}")
output_blocks.append(f"  DAILY ROI BREAKDOWN — How each day performed individually")
output_blocks.append(f"  Each day uses a fresh $1,000 bankroll split evenly across that day's trades.")
output_blocks.append(f"  Cumulative = running total of daily net P&L as % of $1,000.")
output_blocks.append(f"{'='*100}")

output_blocks.append(f"\n  ── Strategy 1: All Unfiltered (current config) ──")
output_blocks.append(f"  {'Date':<12s}  |  {'Trades':>9s}  |  {'Record':>15s}  |  {'Day P&L':>27s}  |  {'Cumulative':>40s}")
output_blocks.append(f"  {'-'*130}")
output_blocks.append(daily_breakdown(unfiltered_settled, "Strategy 1"))

output_blocks.append(f"\n  ── Strategy 2: Unfiltered, Score >= 70 ──")
output_blocks.append(f"  {'Date':<12s}  |  {'Trades':>9s}  |  {'Record':>15s}  |  {'Day P&L':>27s}  |  {'Cumulative':>40s}")
output_blocks.append(f"  {'-'*130}")
output_blocks.append(daily_breakdown([t for t in unfiltered_settled if t["score"] >= 70], "Strategy 2"))

output_blocks.append(f"\n  ── Strategy 3: Unfiltered, Score >= 80 ──")
output_blocks.append(f"  {'Date':<12s}  |  {'Trades':>9s}  |  {'Record':>15s}  |  {'Day P&L':>27s}  |  {'Cumulative':>40s}")
output_blocks.append(f"  {'-'*130}")
output_blocks.append(daily_breakdown([t for t in unfiltered_settled if t["score"] >= 80], "Strategy 3"))

output_blocks.append(f"\n  ── Strategy 4: Unfiltered, Entry $0.30-$0.70 (best performer) ──")
output_blocks.append(f"  {'Date':<12s}  |  {'Trades':>9s}  |  {'Record':>15s}  |  {'Day P&L':>27s}  |  {'Cumulative':>40s}")
output_blocks.append(f"  {'-'*130}")
output_blocks.append(daily_breakdown([t for t in unfiltered_settled if 0.30 <= t["entry"] <= 0.70], "Strategy 4"))

output_blocks.append(f"\n  ── Strategy 5: All Signals (no filters) ──")
output_blocks.append(f"  {'Date':<12s}  |  {'Trades':>9s}  |  {'Record':>15s}  |  {'Day P&L':>27s}  |  {'Cumulative':>40s}")
output_blocks.append(f"  {'-'*130}")
output_blocks.append(daily_breakdown(all_settled, "Strategy 5"))

output_blocks.append(f"\n  ── Strategy 6: All Signals, Score >= 70 ──")
output_blocks.append(f"  {'Date':<12s}  |  {'Trades':>9s}  |  {'Record':>15s}  |  {'Day P&L':>27s}  |  {'Cumulative':>40s}")
output_blocks.append(f"  {'-'*130}")
output_blocks.append(daily_breakdown([t for t in all_settled if t["score"] >= 70], "Strategy 6"))

output_blocks.append(f"\n  ── Strategy 7: All Signals With Current Filters Re-Applied ──")
output_blocks.append(f"  {'Date':<12s}  |  {'Trades':>9s}  |  {'Record':>15s}  |  {'Day P&L':>27s}  |  {'Cumulative':>40s}")
output_blocks.append(f"  {'-'*130}")
s7_trades = [t for t in all_settled 
             if 0.30 <= t["entry"] <= 0.85 
             and not any("imbal_low" in r for r in t.get("filter_reasons", []))
             and not t["filtered"]]
output_blocks.append(daily_breakdown(s7_trades, "Strategy 7"))

output_blocks.append(f"\n  ── Strategy 8: Filtered-Only Signals (what we rejected) ──")
output_blocks.append(f"  {'Date':<12s}  |  {'Trades':>9s}  |  {'Record':>15s}  |  {'Day P&L':>27s}  |  {'Cumulative':>40s}")
output_blocks.append(f"  {'-'*130}")
output_blocks.append(daily_breakdown(filtered_settled, "Strategy 8"))

# ── Sequential (realistic) simulation ──
output_blocks.append(f"\n\n{'#'*100}")
output_blocks.append(f"{'#'*100}")
output_blocks.append(f"##  SEQUENTIAL SIMULATION — REALISTIC LIVE TRADING MODEL")
output_blocks.append(f"##  Flat $100 per trade from available cash. Capital is LOCKED until event settles.")
output_blocks.append(f"##  When event closes, cost + P&L returns to available cash. Max 10 concurrent trades.")
output_blocks.append(f"##  Starting bankroll: $1,000")
output_blocks.append(f"{'#'*100}")
output_blocks.append(f"{'#'*100}")

# Sequential sim for all 8 strategies
output_blocks.append(format_sequential(
    "SEQ STRATEGY 1: ALL UNFILTERED SIGNALS",
    "$100 per trade, capital locked until settlement.",
    BANKROLL, [t for t in unfiltered_settled if t["score"] >= 70]))

output_blocks.append(format_sequential(
    "SEQ STRATEGY 3: UNFILTERED, SCORE >= 80",
    "$100 per trade, capital locked until settlement.",
    BANKROLL, [t for t in unfiltered_settled if t["score"] >= 80]))

output_blocks.append(format_sequential(
    "SEQ STRATEGY 4: UNFILTERED, ENTRY $0.30\u2013$0.70",
    "$100 per trade, capital locked until settlement.",
    BANKROLL, [t for t in unfiltered_settled if 0.30 <= t["entry"] <= 0.70]))

output_blocks.append(format_sequential(
    "SEQ STRATEGY 5: ALL SIGNALS (no filters) — UNCONSTRAINED",
    "$100 per trade, capital locked until settlement. No daily cap, no slippage, 100% fill.",
    BANKROLL, all_settled))

output_blocks.append(format_sequential(
    "SEQ STRATEGY 5R: ALL SIGNALS (no filters) — REALISTIC",
    "$100 per trade, capital locked. Max 20 trades/day, 2¢ slippage, 80% fill rate.",
    BANKROLL, all_settled, max_trades_per_day=20, slippage_cents=2, fill_rate=0.80))

output_blocks.append(format_sequential(
    "SEQ STRATEGY 6: ALL SIGNALS, SCORE >= 70 — UNCONSTRAINED",
    "$100 per trade, capital locked until settlement. No daily cap, no slippage, 100% fill.",
    BANKROLL, [t for t in all_settled if t["score"] >= 70]))

output_blocks.append(format_sequential(
    "SEQ STRATEGY 6R: ALL SIGNALS, SCORE >= 70 — REALISTIC",
    "$100 per trade, capital locked. Max 20 trades/day, 2¢ slippage, 80% fill rate.",
    BANKROLL, [t for t in all_settled if t["score"] >= 70], max_trades_per_day=20, slippage_cents=2, fill_rate=0.80))

s7_trades_seq = [t for t in all_settled 
                 if 0.30 <= t["entry"] <= 0.85 
                 and not any("imbal_low" in r for r in t.get("filter_reasons", []))
                 and not t["filtered"]]
output_blocks.append(format_sequential(
    "SEQ STRATEGY 7: ALL SIGNALS WITH CURRENT FILTERS RE-APPLIED",
    "$100 per trade, capital locked until settlement.",
    BANKROLL, s7_trades_seq))

output_blocks.append(format_sequential(
    "SEQ STRATEGY 8: FILTERED-ONLY SIGNALS — UNCONSTRAINED",
    "$100 per trade, capital locked until settlement. No daily cap, no slippage, 100% fill.",
    BANKROLL, filtered_settled))

output_blocks.append(format_sequential(
    "SEQ STRATEGY 8R: FILTERED-ONLY SIGNALS — REALISTIC",
    "$100 per trade, capital locked. Max 20 trades/day, 2¢ slippage, 80% fill rate.",
    BANKROLL, filtered_settled, max_trades_per_day=20, slippage_cents=2, fill_rate=0.80))

output_blocks.append(format_sequential(
    "SEQ STRATEGY 9: ALL SIGNALS, SCORE >= 70, ENTRY $0.30–$0.70",
    "$100 per trade, capital locked until settlement. Optimal combo of scoring + price filtering.",
    BANKROLL, [t for t in all_settled if t["score"] >= 70 and 0.30 <= t["entry"] <= 0.70]))

output_blocks.append(format_sequential(
    "SEQ STRATEGY 9R: ALL SIGNALS, SCORE >= 70, ENTRY $0.30–$0.70 — REALISTIC",
    "$100 per trade, capital locked. Max 20 trades/day, 2¢ slippage, 80% fill rate.",
    BANKROLL, [t for t in all_settled if t["score"] >= 70 and 0.30 <= t["entry"] <= 0.70], max_trades_per_day=20, slippage_cents=2, fill_rate=0.80))

# Sequential daily breakdown
output_blocks.append(f"\n{'='*100}")
output_blocks.append(f"  SEQUENTIAL DAILY BREAKDOWN — Flat $100 per trade, capital locked until settlement")
output_blocks.append(f"  Capital returns only when event settles. Skipped = not enough available cash.")
output_blocks.append(f"{'='*100}")

seq_strat_map = {
    "Strategy 1": unfiltered_settled,
    "Strategy 2": [t for t in unfiltered_settled if t["score"] >= 70],
    "Strategy 3": [t for t in unfiltered_settled if t["score"] >= 80],
    "Strategy 4": [t for t in unfiltered_settled if 0.30 <= t["entry"] <= 0.70],
    "Strategy 5": all_settled,
    "Strategy 6": [t for t in all_settled if t["score"] >= 70],
    "Strategy 7": s7_trades_seq,
    "Strategy 8": filtered_settled,
    "Strategy 9": [t for t in all_settled if t["score"] >= 70 and 0.30 <= t["entry"] <= 0.70],
}
for label, strades in seq_strat_map.items():
    output_blocks.append(f"\n  ── Seq {label} ──")
    output_blocks.append(f"  {'Date':<12s}  |  {'Trades':>9s}  |  {'Record':>15s}  |  {'Day P&L':>27s}  |  {'Cumulative':>40s}")
    output_blocks.append(f"  {'-'*130}")
    output_blocks.append(sequential_daily_breakdown(strades, label))

# ── Strategy comparison summary table ──
output_blocks.append(f"\n\n{'='*100}")
output_blocks.append(f"  STRATEGY COMPARISON — Even-Split vs Sequential (Compounding)")
output_blocks.append(f"{'='*100}")
output_blocks.append(f"  {'Strategy':<55s} | {'Even-Split ROI':>14s} | {'Seq ROI':>10s} | {'Seq End Bal':>12s} | {'Max DD':>7s} | {'W/L':>10s}")
output_blocks.append(f"  {'-'*120}")

comp_strategies = [
    ("S1: All Unfiltered", unfiltered_settled),
    ("S2: Unfiltered, Score>=70", [t for t in unfiltered_settled if t["score"] >= 70]),
    ("S3: Unfiltered, Score>=80", [t for t in unfiltered_settled if t["score"] >= 80]),
    ("S4: Unfiltered, Entry $0.30-$0.70", [t for t in unfiltered_settled if 0.30 <= t["entry"] <= 0.70]),
    ("S4-75: Unfiltered, Score>=75, Entry $0.30-$0.70", [t for t in unfiltered_settled if t["score"] >= 75 and 0.30 <= t["entry"] <= 0.70]),
    ("S4W: Unfiltered, Entry $0.25-$0.75", [t for t in unfiltered_settled if 0.25 <= t["entry"] <= 0.75]),
    ("S5: All Signals (no filters)", all_settled),
    ("S6: All Signals, Score>=70", [t for t in all_settled if t["score"] >= 70]),
    ("S6E: All Signals, Score>=70, Entry $0.30-$0.70", [t for t in all_settled if t["score"] >= 70 and 0.30 <= t["entry"] <= 0.70]),
    ("S7: Filters Re-Applied", s7_trades_seq),
    ("S8: Filtered-Only (rejected)", filtered_settled),
]
for label, strades in comp_strategies:
    even = calc_strategy(strades, BANKROLL)
    seq = sequential_sim(strades, BANKROLL, 100.0)
    if even and seq:
        total = seq['wins'] + seq['losses']
        wr = seq['wins'] / total * 100 if total else 0
        output_blocks.append(
            f"  {label:<55s} | {even['roi']:>+13.1f}% | {seq['roi']:>+9.1f}% | ${seq['balance']:>10,.2f} | {seq['max_dd']:>6.1f}% | {seq['wins']}W/{seq['losses']}L ({wr:.0f}%)"
        )
    else:
        output_blocks.append(f"  {label:<55s} | {'N/A':>14s} | {'N/A':>10s} | {'N/A':>12s} | {'N/A':>7s} | N/A")

# ── Realistic comparison (with constraints) ──
output_blocks.append(f"\n\n{'='*100}")
output_blocks.append(f"  REALISTIC COMPARISON — Max 20 trades/day, 2¢ slippage, 80% fill rate")
output_blocks.append(f"{'='*100}")
output_blocks.append(f"  {'Strategy':<55s} | {'Seq ROI':>10s} | {'Seq End Bal':>12s} | {'Max DD':>7s} | {'Trades':>8s} | {'W/L':>15s}")
output_blocks.append(f"  {'-'*120}")

realistic_strategies = [
    ("S4: Unfiltered, Entry $0.30-$0.70", [t for t in unfiltered_settled if 0.30 <= t["entry"] <= 0.70]),
    ("S4-75: Unfiltered, Score>=75, Entry $0.30-$0.70", [t for t in unfiltered_settled if t["score"] >= 75 and 0.30 <= t["entry"] <= 0.70]),
    ("S4W: Unfiltered, Entry $0.25-$0.75", [t for t in unfiltered_settled if 0.25 <= t["entry"] <= 0.75]),
    ("S5R: All Signals (realistic)", all_settled),
    ("S6R: All Signals, Score>=70 (realistic)", [t for t in all_settled if t["score"] >= 70]),
    ("S6ER: All Signals, Score>=70, Entry $0.30-$0.70 (realistic)", [t for t in all_settled if t["score"] >= 70 and 0.30 <= t["entry"] <= 0.70]),
    ("S7: Filters Re-Applied", s7_trades_seq),
    ("S8R: Filtered-Only (realistic)", filtered_settled),
]
for label, strades in realistic_strategies:
    if "realistic" in label.lower():
        seq = sequential_sim(strades, BANKROLL, 100.0, max_trades_per_day=20, slippage_cents=2, fill_rate=0.80)
    else:
        seq = sequential_sim(strades, BANKROLL, 100.0)
    if seq:
        total = seq['wins'] + seq['losses']
        wr = seq['wins'] / total * 100 if total else 0
        output_blocks.append(
            f"  {label:<55s} | {seq['roi']:>+9.1f}% | ${seq['balance']:>10,.2f} | {seq['max_dd']:>6.1f}% | {seq['n']:>8d} | {seq['wins']}W/{seq['losses']}L ({wr:.0f}%)"
        )
    else:
        output_blocks.append(f"  {label:<55s} | {'N/A':>10s} | {'N/A':>12s} | {'N/A':>7s} | {'N/A':>8s} | N/A")


output_blocks.append("")  # trailing newline

full_output = "\n".join(output_blocks) + "\n"

outfile = "edge_roi_analysis.txt"
with open(outfile, "w", encoding="utf-8") as f:
    f.write(full_output)
print(f"\nResults written to {outfile} ({len(full_output)} bytes)", flush=True)
print("DONE", flush=True)
