"""Deep forensic analysis of edge scanner signals vs actual outcomes.

Answers: EXACTLY what features distinguish winning signals from losing signals?
Goes beyond score buckets to examine each component, entry price, EV, fee impact,
event correlation, and signal timing.
"""
import json, os, time, base64, requests
from datetime import datetime, timezone
from collections import defaultdict
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from config import *

# --- API setup ---
host = "https://demo-api.kalshi.co" if MODE == "demo" else "https://api.elections.kalshi.com"
api_prefix = "/trade-api/v2"
with open(KALSHI_PRIVATE_KEY_PATH, "rb") as f:
    private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def create_signature(pk, ts, method, path):
    msg = f"{ts}{method}{path.split('?')[0]}".encode('utf-8')
    sig = pk.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return base64.b64encode(sig).decode('utf-8')

def signed_request(method, path, params=None):
    ts = str(int(time.time() * 1000))
    full_path = api_prefix + path
    if params:
        full_path += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    sig = create_signature(private_key, ts, method.upper(), api_prefix + path)
    headers = {"KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID, "KALSHI-ACCESS-SIGNATURE": sig, "KALSHI-ACCESS-TIMESTAMP": ts}
    url = f"{host}{full_path}"
    resp = requests.get(url, headers=headers, timeout=10) if method.upper() == "GET" else requests.post(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()

# --- Load signals ---
signal_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "edge_signals")
signals = []
for fname in sorted(os.listdir(signal_dir)):
    if fname.endswith('.jsonl'):
        with open(os.path.join(signal_dir, fname), 'r') as f:
            for line in f:
                line = line.strip()
                if line:
                    signals.append(json.loads(line))
print(f"Loaded {len(signals)} signals")

# --- Deduplicate: keep first signal per ticker+direction (the ENTRY signal) ---
first_signals = {}
for s in signals:
    key = (s['ticker'], s['direction'])
    if key not in first_signals:
        first_signals[key] = s
print(f"Unique ticker+direction combos: {len(first_signals)}")

# --- Fetch settlements ---
ticker_results = {}
unique_tickers = set(s['ticker'] for s in signals)
for ticker in unique_tickers:
    if ticker in ticker_results:
        continue
    try:
        data = signed_request("GET", f"/markets/{ticker}")
        m = data.get('market', data)
        result = m.get('result', '')
        status = m.get('status', '')
        ticker_results[ticker] = {'result': result, 'status': status}
    except Exception as e:
        print(f"  FAILED {ticker}: {e}")
    time.sleep(0.05)

# --- Join: for each first signal, determine win/loss ---
trades = []
for (ticker, direction), sig in first_signals.items():
    tr = ticker_results.get(ticker)
    if not tr or tr['status'] not in ('finalized', 'determined', 'closed'):
        continue

    result = tr['result'].lower()
    if not result or result not in ('yes', 'no'):
        continue

    det = sig.get('details', {})
    entry_price = det.get('best_bid', 0)
    if entry_price <= 0:
        continue

    won = (direction == "YES" and result == "yes") or (direction == "NO" and result == "no")
    gross_pnl = (1.0 - entry_price) if won else (-entry_price)
    fee = 0.02  # ~2c per contract
    net_pnl = gross_pnl - fee

    # Compute EV: what was the breakeven probability?
    # Break-even P(win) = (entry_price + fee) / 1.0
    breakeven_prob = entry_price + fee
    # Implied edge = actual win rate at this score (unknown per-trade, but we can record it)

    trades.append({
        'ticker': ticker,
        'direction': direction,
        'score': sig['score'],
        'entry_price': entry_price,
        'won': won,
        'gross_pnl': gross_pnl,
        'net_pnl': net_pnl,
        'fee': fee,
        'breakeven_prob': breakeven_prob,
        'max_return_pct': (1.0 - entry_price) / entry_price * 100,
        'imbalance_pts': det.get('imbalance_pts', 0),
        'spread_pts': det.get('spread_pts', 0),
        'top_pts': det.get('top_pts', 0),
        'flow_pts': det.get('flow_pts', 0),
        'spread_cents': det.get('spread_cents', 0),
        'best_bid_size': det.get('best_bid_size', 0),
        'flow_30s': det.get('flow_30s', 0),
        'imbalance': det.get('imbalance', 0),
        'our_depth': det.get('our_depth', 0),
        'opp_depth': det.get('opp_depth', 0),
        'event': ticker.rsplit('-', 1)[0] if '-' in ticker else ticker,
        'close_time': sig.get('close_time', ''),
        'signal_ts': sig.get('ts', ''),
    })

print(f"\nSettled first-signal trades: {len(trades)}")
wins = [t for t in trades if t['won']]
losses = [t for t in trades if not t['won']]
print(f"  Wins: {len(wins)}  Losses: {len(losses)}")

# =================================================================
# ANALYSIS 1: Component-by-component comparison (winners vs losers)
# =================================================================
print("\n" + "="*80)
print("  ANALYSIS 1: COMPONENT COMPARISON -- WINNERS vs LOSERS")
print("="*80)
components = ['score', 'imbalance_pts', 'spread_pts', 'top_pts', 'flow_pts',
              'entry_price', 'spread_cents', 'best_bid_size', 'flow_30s', 'imbalance',
              'our_depth', 'opp_depth', 'max_return_pct']
for comp in components:
    w_vals = [t[comp] for t in wins if t[comp] is not None]
    l_vals = [t[comp] for t in losses if t[comp] is not None]
    w_avg = sum(w_vals) / len(w_vals) if w_vals else 0
    l_avg = sum(l_vals) / len(l_vals) if l_vals else 0
    diff = w_avg - l_avg
    print(f"  {comp:20s}  WIN avg: {w_avg:8.2f}  LOSS avg: {l_avg:8.2f}  diff: {diff:+8.2f}  {'<-- USEFUL' if abs(diff) > 0.5 else ''}")

# =================================================================
# ANALYSIS 2: Entry price breakdown with win rate and NET EV
# =================================================================
print("\n" + "="*80)
print("  ANALYSIS 2: ENTRY PRICE vs WIN RATE vs NET PROFIT PER CONTRACT")
print("="*80)
price_buckets = [
    ("$0.01-0.19", 0.01, 0.19),
    ("$0.20-0.49", 0.20, 0.49),
    ("$0.50-0.69", 0.50, 0.69),
    ("$0.70-0.84", 0.70, 0.84),
    ("$0.85-0.94", 0.85, 0.94),
    ("$0.95-1.00", 0.95, 1.00),
]
for label, lo, hi in price_buckets:
    bucket = [t for t in trades if lo <= t['entry_price'] <= hi]
    if not bucket:
        print(f"  {label}: no trades")
        continue
    w = sum(1 for t in bucket if t['won'])
    n = len(bucket)
    wr = w / n * 100
    avg_net = sum(t['net_pnl'] for t in bucket) / n
    avg_gross = sum(t['gross_pnl'] for t in bucket) / n
    avg_fee = sum(t['fee'] for t in bucket) / n
    avg_maxret = sum(t['max_return_pct'] for t in bucket) / n
    print(f"  {label}: {n:3d} trades | {w}W/{n-w}L ({wr:5.1f}%) | "
          f"avg gross: ${avg_gross:+.3f} | avg fee: ${avg_fee:.3f} | "
          f"avg net: ${avg_net:+.3f}/contract | max return: {avg_maxret:.1f}%")

# =================================================================
# ANALYSIS 3: Profit-adjusted scoring -- what if we weighted by EV?
# =================================================================
print("\n" + "="*80)
print("  ANALYSIS 3: EXPECTED VALUE ANALYSIS")
print("="*80)
for t in trades:
    # For each trade: what confidence was NEEDED to break even?
    t['needed_confidence'] = t['breakeven_prob']
    # What confidence did the score IMPLY? (rough calibration from data)
    # We'll compute this from the actual data per score bucket
score_buckets = [(60,64), (65,69), (70,74), (75,79), (80,84), (85,89), (90,100)]
print(f"  {'Score':>8s}  {'Trades':>6s}  {'WinRate':>7s}  {'AvgEntry':>8s}  {'NeededConf':>10s}  {'Surplus':>8s}  {'AvgNetPnL':>9s}  {'$100 bet':>8s}")
for lo, hi in score_buckets:
    bucket = [t for t in trades if lo <= t['score'] <= hi]
    if not bucket:
        continue
    n = len(bucket)
    w = sum(1 for t in bucket if t['won'])
    wr = w / n
    avg_entry = sum(t['entry_price'] for t in bucket) / n
    avg_needed = sum(t['needed_confidence'] for t in bucket) / n
    surplus = wr - avg_needed  # positive = profitable edge
    avg_net = sum(t['net_pnl'] for t in bucket) / n
    bet_100 = avg_net / avg_entry * 100  # scale to $100 bet
    print(f"  {lo:3d}-{hi:3d}  {n:6d}  {wr:6.1%}  ${avg_entry:7.3f}  {avg_needed:9.1%}  {surplus:+7.1%}  ${avg_net:+8.3f}  ${bet_100:+7.2f}")

# =================================================================
# ANALYSIS 4: Event-level correlation analysis
# =================================================================
print("\n" + "="*80)
print("  ANALYSIS 4: EVENT CORRELATION -- CONCENTRATED vs DIVERSIFIED")
print("="*80)
event_trades = defaultdict(list)
for t in trades:
    event_trades[t['event']].append(t)

for event, ev_trades in sorted(event_trades.items(), key=lambda x: -len(x[1])):
    n = len(ev_trades)
    w = sum(1 for t in ev_trades if t['won'])
    net = sum(t['net_pnl'] for t in ev_trades)
    dirs = set(t['direction'] for t in ev_trades)
    strikes = len(ev_trades)
    print(f"  {event:45s} | {n:2d} trades | {w}W/{n-w}L | net: ${net:+.3f}/ct | dirs: {','.join(dirs)}")

# =================================================================
# ANALYSIS 5: The SINGLE BEST trade identifier
# =================================================================
print("\n" + "="*80)
print("  ANALYSIS 5: WHAT MAKES THE BEST TRADES?")
print("="*80)
# Sort by net PnL descending
by_pnl = sorted(trades, key=lambda t: t['net_pnl'], reverse=True)
print("  TOP 5 MOST PROFITABLE:")
for t in by_pnl[:5]:
    print(f"    {t['ticker']:45s} {t['direction']:3s} score={t['score']:4.0f} entry=${t['entry_price']:.3f} "
          f"net=${t['net_pnl']:+.3f} imbal={t['imbalance_pts']:.0f} spread={t['spread_pts']:.0f} "
          f"top={t['top_pts']:.0f} flow={t['flow_pts']:.0f} maxret={t['max_return_pct']:.0f}%")
print("  BOTTOM 5 MOST LOSING:")
for t in by_pnl[-5:]:
    print(f"    {t['ticker']:45s} {t['direction']:3s} score={t['score']:4.0f} entry=${t['entry_price']:.3f} "
          f"net=${t['net_pnl']:+.3f} imbal={t['imbalance_pts']:.0f} spread={t['spread_pts']:.0f} "
          f"top={t['top_pts']:.0f} flow={t['flow_pts']:.0f} maxret={t['max_return_pct']:.0f}%")

# =================================================================
# ANALYSIS 6: Fee-adjusted profitability by entry price + score
# =================================================================
print("\n" + "="*80)
print("  ANALYSIS 6: 2D HEATMAP -- SCORE x ENTRY PRICE -> NET EV per $1 risked")
print("="*80)
score_ranges = [(60,69), (70,79), (80,100)]
price_ranges = [("$0.01-0.49", 0.01, 0.49), ("$0.50-0.79", 0.50, 0.79), ("$0.80-0.94", 0.80, 0.94), ("$0.95-1.00", 0.95, 1.00)]
print(f"  {'':20s}", end="")
for slo, shi in score_ranges:
    print(f"  Score {slo}-{shi:3d}", end="")
print()
for plabel, plo, phi in price_ranges:
    print(f"  {plabel:20s}", end="")
    for slo, shi in score_ranges:
        bucket = [t for t in trades if slo <= t['score'] <= shi and plo <= t['entry_price'] <= phi]
        if not bucket:
            print(f"  {'---':>12s}", end="")
        else:
            n = len(bucket)
            w = sum(1 for t in bucket if t['won'])
            avg_net = sum(t['net_pnl'] for t in bucket) / n
            ev_per_dollar = avg_net / (sum(t['entry_price'] for t in bucket) / n)
            print(f"  {n}t {w}W {ev_per_dollar:+.0%}".rjust(12), end="")
    print()

# =================================================================
# ANALYSIS 7: What if we used a SIMPLE rule: entry + spread + direction only?
# =================================================================
print("\n" + "="*80)
print("  ANALYSIS 7: SIMPLE RULE BACKTESTS")
print("="*80)

rules = [
    ("Score>=70 & entry<=0.90", lambda t: t['score'] >= 70 and t['entry_price'] <= 0.90),
    ("Score>=70 & entry<=0.85", lambda t: t['score'] >= 70 and t['entry_price'] <= 0.85),
    ("Score>=70 & entry 0.50-0.85", lambda t: t['score'] >= 70 and 0.50 <= t['entry_price'] <= 0.85),
    ("Score>=65 & entry 0.50-0.85", lambda t: t['score'] >= 65 and 0.50 <= t['entry_price'] <= 0.85),
    ("Score>=60 & entry 0.50-0.85", lambda t: t['score'] >= 60 and 0.50 <= t['entry_price'] <= 0.85),
    ("Imbalance>=15 & entry<=0.85", lambda t: t['imbalance_pts'] >= 15 and t['entry_price'] <= 0.85),
    ("Imbalance>=20 & entry<=0.85", lambda t: t['imbalance_pts'] >= 20 and t['entry_price'] <= 0.85),
    ("Imbalance>=15 & spread<=2c", lambda t: t['imbalance_pts'] >= 15 and t['spread_cents'] <= 2),
    ("Top>=15 & entry 0.30-0.85", lambda t: t['top_pts'] >= 15 and 0.30 <= t['entry_price'] <= 0.85),
    ("Flow>=15 & entry 0.30-0.85", lambda t: t['flow_pts'] >= 15 and 0.30 <= t['entry_price'] <= 0.85),
    ("MaxReturn>=20% & score>=65", lambda t: t['max_return_pct'] >= 20 and t['score'] >= 65),
    ("MaxReturn>=30% & score>=65", lambda t: t['max_return_pct'] >= 30 and t['score'] >= 65),
    ("MaxReturn>=15% & score>=70", lambda t: t['max_return_pct'] >= 15 and t['score'] >= 70),
    ("Entry 0.60-0.85 any score", lambda t: 0.60 <= t['entry_price'] <= 0.85),
    ("Best-per-event score>=65", None),  # special handling
]

for name, rule_fn in rules:
    if name == "Best-per-event score>=65":
        # Pick the single best trade per event (highest score, break ties by max return)
        event_best = {}
        for t in trades:
            if t['score'] < 65:
                continue
            ev = t['event']
            if ev not in event_best or t['score'] > event_best[ev]['score'] or \
               (t['score'] == event_best[ev]['score'] and t['max_return_pct'] > event_best[ev]['max_return_pct']):
                event_best[ev] = t
        filtered = list(event_best.values())
    else:
        filtered = [t for t in trades if rule_fn(t)]

    if not filtered:
        print(f"  {name:40s} | 0 trades")
        continue

    n = len(filtered)
    w = sum(1 for t in filtered if t['won'])
    total_net = sum(t['net_pnl'] for t in filtered)
    avg_net = total_net / n
    # Simulate $1000 spread evenly
    per_trade = 1000.0 / n
    sim_net = sum((per_trade / t['entry_price']) * t['net_pnl'] for t in filtered)
    print(f"  {name:40s} | {n:2d}t | {w}W/{n-w}L ({w/n*100:4.0f}%) | "
          f"avg net: ${avg_net:+.3f}/ct | $1k sim: ${sim_net:+.0f} ({sim_net/10:+.1f}%)")

# =================================================================
# ANALYSIS 8: Direction analysis -- are YES or NO signals better?
# =================================================================
print("\n" + "="*80)
print("  ANALYSIS 8: YES vs NO DIRECTION")
print("="*80)
for d in ["YES", "NO"]:
    dt = [t for t in trades if t['direction'] == d]
    if not dt:
        continue
    n = len(dt)
    w = sum(1 for t in dt if t['won'])
    avg_entry = sum(t['entry_price'] for t in dt) / n
    avg_net = sum(t['net_pnl'] for t in dt) / n
    print(f"  {d}: {n} trades | {w}W/{n-w}L ({w/n*100:.0f}%) | avg entry: ${avg_entry:.3f} | avg net: ${avg_net:+.3f}")

# YES at different entry prices
for d in ["YES", "NO"]:
    print(f"  {d} by entry price:")
    for label, lo, hi in [("<$0.50", 0.01, 0.49), ("$0.50-0.79", 0.50, 0.79), ("$0.80-0.94", 0.80, 0.94), ("$0.95+", 0.95, 1.00)]:
        bucket = [t for t in trades if t['direction'] == d and lo <= t['entry_price'] <= hi]
        if not bucket:
            continue
        n = len(bucket)
        w = sum(1 for t in bucket if t['won'])
        avg_net = sum(t['net_pnl'] for t in bucket) / n
        print(f"    {label:15s}: {n}t | {w}W/{n-w}L ({w/n*100:.0f}%) | avg net: ${avg_net:+.3f}")

# =================================================================
# ANALYSIS 9: Per-contract net P&L distribution
# =================================================================
print("\n" + "="*80)
print("  ANALYSIS 9: P&L DISTRIBUTION (sorted by net per contract)")
print("="*80)
for t in sorted(trades, key=lambda t: t['net_pnl'], reverse=True):
    marker = "WIN " if t['won'] else "LOSS"
    print(f"  {marker} {t['ticker']:45s} {t['direction']:3s} score={t['score']:4.0f} "
          f"entry=${t['entry_price']:.3f} gross=${t['gross_pnl']:+.3f} net=${t['net_pnl']:+.3f} "
          f"maxret={t['max_return_pct']:5.1f}% imb={t['imbalance_pts']:4.0f} sp={t['spread_pts']:4.0f} "
          f"top={t['top_pts']:4.0f} flw={t['flow_pts']:4.0f}")

print("\n\nDONE")

# Also write all output to file
import io, sys

