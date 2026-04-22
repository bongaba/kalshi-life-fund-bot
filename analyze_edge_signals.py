"""Deep analysis of edge scanner signal data."""
import json, statistics, sys
from collections import defaultdict
from datetime import datetime

signals = []
with open("logs/edge_signals/edge_signals_20260410.jsonl") as f:
    for line in f:
        if line.strip():
            signals.append(json.loads(line.strip()))

print(f"Total signals: {len(signals)}")

# Component distributions
imb_pts = [s['details']['imbalance_pts'] for s in signals]
spread_pts = [s['details']['spread_pts'] for s in signals]
top_pts = [s['details']['top_pts'] for s in signals]
flow_pts = [s['details']['flow_pts'] for s in signals]

print("\n=== Component distribution across all signals ===")
maxvals = {'imbalance': 35, 'spread': 25, 'top': 20, 'flow': 20}
for name, vals in [('imbalance', imb_pts), ('spread', spread_pts), ('top', top_pts), ('flow', flow_pts)]:
    zero = sum(1 for v in vals if v == 0)
    maxed = sum(1 for v in vals if v >= maxvals[name] * 0.95)
    avg = sum(vals)/len(vals)
    print(f"  {name:>10}: avg={avg:5.1f}/{maxvals[name]} | zero={zero:3d} ({zero/len(vals)*100:.0f}%) | near-max={maxed:3d} ({maxed/len(vals)*100:.0f}%)")

print(f"\n  flow_30s == 0: {sum(1 for s in signals if s['details']['flow_30s'] == 0)} / {len(signals)}")

# Per-ticker fire count
print("\n=== Per-ticker signal count (top 15 most frequent) ===")
ticker_count = defaultdict(int)
for s in signals:
    ticker_count[(s['ticker'], s['direction'])] += 1
for (t,d), c in sorted(ticker_count.items(), key=lambda x: -x[1])[:15]:
    print(f"  {c:>3}x  {t} {d}")

# Time gaps
print("\n=== Time span of repeated signals ===")
ticker_times = defaultdict(list)
for s in signals:
    ts = datetime.fromisoformat(s['ts'])
    ticker_times[(s['ticker'], s['direction'])].append(ts)
for (t,d), times in sorted(ticker_times.items()):
    if len(times) > 1:
        gap = (max(times) - min(times)).total_seconds() / 60
        print(f"  {t} {d}: {len(times)} signals over {gap:.0f} min")

# Correlation
print("\n=== Component correlations ===")
def corr(a, b):
    n = len(a)
    ma, mb = statistics.mean(a), statistics.mean(b)
    sa, sb = statistics.stdev(a), statistics.stdev(b)
    if sa == 0 or sb == 0:
        return 0
    return sum((x-ma)*(y-mb) for x,y in zip(a,b)) / (n * sa * sb)

pairs = [('imbalance','spread'), ('imbalance','top'), ('imbalance','flow'), ('spread','top'), ('spread','flow'), ('top','flow')]
comp = {'imbalance': imb_pts, 'spread': spread_pts, 'top': top_pts, 'flow': flow_pts}
for a,b in pairs:
    r = corr(comp[a], comp[b])
    print(f"  {a:>10} vs {b:<10}: r={r:+.3f}")

# Score vs entry price
print("\n=== Score distribution by entry price bucket ===")
price_buckets = defaultdict(list)
for s in signals:
    d = s['direction']
    entry = s.get('yes_best_bid', 0) if d == 'YES' else s.get('no_best_bid', 0)
    if entry and entry > 0:
        if entry >= 0.95: bucket = '0.95-1.00'
        elif entry >= 0.80: bucket = '0.80-0.94'
        elif entry >= 0.50: bucket = '0.50-0.79'
        elif entry >= 0.20: bucket = '0.20-0.49'
        else: bucket = '0.01-0.19'
        price_buckets[bucket].append(s['score'])

for bucket in ['0.01-0.19', '0.20-0.49', '0.50-0.79', '0.80-0.94', '0.95-1.00']:
    scores = price_buckets.get(bucket, [])
    if scores:
        print(f"  Entry {bucket}: {len(scores):>3} signals | avg_score={statistics.mean(scores):.1f} | min={min(scores):.0f} | max={max(scores):.0f}")

# Market correlation analysis
print("\n=== Correlated markets (same event, multiple strikes) ===")
event_map = defaultdict(list)
for s in signals:
    t = s['ticker']
    # Extract event base: e.g. KXBTC-26APR1017 from KXBTC-26APR1017-B70250
    parts = t.rsplit('-', 1)
    if len(parts) == 2:
        event_map[parts[0]].append((t, s['direction'], s['score']))
for event, trades in sorted(event_map.items()):
    unique = set((t,d) for t,d,s in trades)
    if len(unique) > 2:
        directions = defaultdict(list)
        for t,d,s in trades:
            directions[(t,d)].append(s)
        print(f"  {event}: {len(unique)} unique ticker+dir combos")
        for (t,d), scores in sorted(directions.items()):
            print(f"    {t} {d}: avg={statistics.mean(scores):.1f} ({len(scores)} signals)")

# Directional consistency — do markets flip direction?
print("\n=== Direction flips (same ticker signals both YES and NO) ===")
ticker_dirs = defaultdict(set)
for s in signals:
    ticker_dirs[s['ticker']].add(s['direction'])
both = {t for t, ds in ticker_dirs.items() if len(ds) == 2}
print(f"  Tickers signaling BOTH directions: {len(both)} / {len(ticker_dirs)} ({len(both)/len(ticker_dirs)*100:.0f}%)")
for t in sorted(both):
    yes_scores = [s['score'] for s in signals if s['ticker'] == t and s['direction'] == 'YES']
    no_scores = [s['score'] for s in signals if s['ticker'] == t and s['direction'] == 'NO']
    print(f"    {t}: YES avg={statistics.mean(yes_scores):.1f}({len(yes_scores)}) | NO avg={statistics.mean(no_scores):.1f}({len(no_scores)})")

# Time-to-close analysis — does score correlate with proximity to close?
print("\n=== Score vs time-to-close ===")
ttc_buckets = defaultdict(list)
for s in signals:
    close = datetime.fromisoformat(s.get('close_time', '').replace('Z', '+00:00'))
    sig_time = datetime.fromisoformat(s['ts'])
    ttc_hours = (close - sig_time).total_seconds() / 3600
    if ttc_hours < 0.5: bucket = '<30min'
    elif ttc_hours < 1: bucket = '30-60min'
    elif ttc_hours < 2: bucket = '1-2hr'
    elif ttc_hours < 4: bucket = '2-4hr'
    else: bucket = '4hr+'
    ttc_buckets[bucket].append(s['score'])

for bucket in ['<30min', '30-60min', '1-2hr', '2-4hr', '4hr+']:
    scores = ttc_buckets.get(bucket, [])
    if scores:
        print(f"  TTC {bucket:>8}: {len(scores):>3} signals | avg_score={statistics.mean(scores):.1f}")

# Spread analysis - how many have 0-cent spread (crossed books)?
print("\n=== Spread distribution ===")
spread_dist = defaultdict(int)
for s in signals:
    sc = s['details']['spread_cents']
    spread_dist[sc] += 1
for cents in sorted(spread_dist):
    print(f"  {cents}c spread: {spread_dist[cents]} signals ({spread_dist[cents]/len(signals)*100:.0f}%)")
