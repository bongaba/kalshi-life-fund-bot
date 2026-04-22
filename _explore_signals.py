"""Quick exploration of signal log data for stop loss simulation feasibility."""
import json, os
from collections import defaultdict, Counter

signals = []
for fname in sorted(os.listdir('logs/edge_signals')):
    if not fname.endswith('.jsonl'):
        continue
    with open(os.path.join('logs/edge_signals', fname)) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                signals.append(json.loads(line))
            except:
                continue

print(f"Total signals: {len(signals)}")
print(f"Sample signal keys: {list(signals[0].keys())}")
print()

# Check how many tickers have multiple signals
by_ticker = defaultdict(list)
for s in signals:
    by_ticker[s['ticker']].append(s)

multi = {k: v for k, v in by_ticker.items() if len(v) > 1}
print(f"Tickers with 1 signal: {sum(1 for v in by_ticker.values() if len(v)==1)}")
print(f"Tickers with 2+ signals: {len(multi)}")
print(f"Max signals for one ticker: {max(len(v) for v in by_ticker.values())}")
print()

# Distribution
dist = Counter(len(v) for v in by_ticker.values())
for count in sorted(dist.keys())[:15]:
    print(f"  {count} signals: {dist[count]} tickers")

# Show sample multi-signal tickers
shown = 0
for tk, sigs in sorted(multi.items()):
    if len(sigs) >= 5:
        sigs_sorted = sorted(sigs, key=lambda x: x['ts'])
        print(f"\nSample: {tk} ({len(sigs)} signals)")
        for s in sigs_sorted[:8]:
            filt = "FILT" if s.get('filtered') else "LIVE"
            print(f"  {s['ts']}  {s.get('direction','?'):>3s}  yes=${s.get('yes_price',0):.2f}  no=${s.get('no_price',0):.2f}  score={int(s.get('score',0)):3d}  {filt}  reasons={s.get('filter_reasons', [])}")
        shown += 1
        if shown >= 3:
            break

# For S4 trades: how many have subsequent signals we could use as "price checks"?
print("\n--- S4 Trade Price Tracking ---")
# Get first unfiltered signal per ticker as "entry"
entries = {}
for s in sorted(signals, key=lambda x: x['ts']):
    tk = s['ticker']
    if not s.get('filtered') and tk not in entries:
        entry_price = s.get('yes_price', 0) if s.get('direction') == 'YES' else s.get('no_price', 0)
        if 0.30 <= entry_price <= 0.70:
            entries[tk] = s

print(f"S4-eligible entries (first unfiltered, $0.30-$0.70): {len(entries)}")
has_followup = 0
for tk in entries:
    subsequent = [s for s in by_ticker[tk] if s['ts'] > entries[tk]['ts']]
    if subsequent:
        has_followup += 1
print(f"Of those, tickers with subsequent signals: {has_followup}")
print(f"Tickers with NO subsequent data: {len(entries) - has_followup}")
