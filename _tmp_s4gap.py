import json, os

signals = []
for fname in sorted(os.listdir('logs/edge_signals')):
    if not fname.endswith('.jsonl'): continue
    with open(os.path.join('logs/edge_signals', fname)) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try: signals.append(json.loads(line))
            except: continue

by_ticker = {}
for s in signals:
    tk = s['ticker']
    if tk not in by_ticker or s['ts'] > by_ticker[tk]['ts']:
        by_ticker[tk] = s

cache_file = 'logs/edge_signals/_market_cache.json'
with open(cache_file) as f:
    cache = json.load(f)

# S4-only trades: score 60-69, entry 0.30-0.70, unfiltered
s4_only = []
for tk, sig in by_ticker.items():
    if sig.get('filtered'): continue
    score = sig.get('score', 0)
    d = sig['direction']
    entry = sig.get('yes_price', 0) if d == 'YES' else sig.get('no_price', 0)
    if score < 60 or score >= 70: continue
    if entry < 0.30 or entry > 0.70: continue
    mkt = cache.get(tk)
    if not mkt or mkt.get('status') not in ('finalized', 'settled'): continue
    result = mkt.get('result', '')
    won = (d == 'YES' and result == 'yes') or (d == 'NO' and result == 'no')
    s4_only.append({'ticker': tk, 'score': score, 'entry': entry, 'won': won, 'direction': d})

wins = sum(1 for t in s4_only if t['won'])
losses = sum(1 for t in s4_only if not t['won'])
total = wins + losses
wr = wins / total * 100 if total else 0
print(f"S4-only trades (score 60-69, entry $0.30-$0.70): {total}")
print(f"  Wins: {wins}, Losses: {losses}, Win rate: {wr:.0f}%")
for t in s4_only:
    tag = "WIN" if t['won'] else "LOSS"
    print(f"  {tag} | {t['ticker']} | {t['direction']} | score={t['score']} | entry=${t['entry']:.3f}")
