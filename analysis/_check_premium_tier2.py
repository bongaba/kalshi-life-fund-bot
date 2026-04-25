"""Check WR/ROI for proposed PREMIUM tier candidates within S6ER baseline."""
import json, os
from datetime import datetime
from collections import defaultdict

SIGNAL_DIR = "logs/edge_signals"
MARKET_CACHE = os.path.join(SIGNAL_DIR, "_market_cache.json")
FEE = 0.02
RISK = 20.0

def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))

raw = []
for fname in sorted(os.listdir(SIGNAL_DIR)):
    if fname.startswith("edge_signals_") and fname.endswith(".jsonl"):
        with open(os.path.join(SIGNAL_DIR, fname)) as f:
            for line in f:
                line = line.strip()
                if line:
                    try: raw.append(json.loads(line))
                    except Exception: pass

by_ticker = {}
for s in raw:
    tk = s["ticker"]
    if tk not in by_ticker or s["ts"] > by_ticker[tk]["ts"]:
        by_ticker[tk] = s

with open(MARKET_CACHE) as f:
    cache = json.load(f)

trades = []
for tk, sig in by_ticker.items():
    score = sig.get("score") or 0
    direction = sig.get("direction")
    entry = sig.get("yes_price") if direction == "YES" else sig.get("no_price")
    if entry is None or score < 70 or not (0.30 <= entry <= 0.70):
        continue
    mkt = cache.get(tk)
    if not mkt or mkt.get("status") not in ("finalized", "settled"):
        continue
    settle = mkt.get("result")
    if settle not in ("yes", "no"):
        continue
    contracts = max(1, int(RISK / entry))
    if (direction == "YES" and settle == "yes") or (direction == "NO" and settle == "no"):
        win, pnl = True, contracts * (1.0 - entry) - contracts * FEE
    else:
        win, pnl = False, -contracts * entry - contracts * FEE
    cost = contracts * entry
    trades.append({
        "date": sig["ts"][:10],
        "score": score,
        "entry": entry,
        "volume": sig.get("volume") or 0,
        "win": win, "pnl": pnl, "cost": cost,
    })

def stats(label, trs):
    if not trs:
        print(f"  {label:<55} N=   0"); return
    n=len(trs); w=sum(1 for t in trs if t["win"])
    net=sum(t["pnl"] for t in trs); cost=sum(t["cost"] for t in trs)
    print(f"  {label:<55} N={n:>4}  W/L={w:>3}/{n-w:<3}  WR={100*w/n:5.1f}%  Net=${net:+9.2f}  ROI={100*net/cost if cost else 0:+6.1f}%")

print(f"Total S6ER baseline trades: {len(trades)}")
print()
print("="*100)
print("PROPOSED PREMIUM TIER variations")
print("="*100)
stats("BASELINE S6ER (all)", trades)
print()
stats("score>=85 + entry<=0.55 + vol>=50k  <-- proposed",
      [t for t in trades if t["score"]>=85 and t["entry"]<=0.55 and t["volume"]>=50000])
stats("score>=85 + entry<=0.55 + vol>=100k",
      [t for t in trades if t["score"]>=85 and t["entry"]<=0.55 and t["volume"]>=100000])
stats("score>=85 + entry<=0.60 + vol>=50k",
      [t for t in trades if t["score"]>=85 and t["entry"]<=0.60 and t["volume"]>=50000])
stats("score>=80 + entry<=0.55 + vol>=50k",
      [t for t in trades if t["score"]>=80 and t["entry"]<=0.55 and t["volume"]>=50000])
stats("score>=90 + entry<=0.55 + vol>=50k",
      [t for t in trades if t["score"]>=90 and t["entry"]<=0.55 and t["volume"]>=50000])
stats("score>=85 + entry<=0.50 + vol>=50k",
      [t for t in trades if t["score"]>=85 and t["entry"]<=0.50 and t["volume"]>=50000])
stats("score>=85 + entry<=0.55 + vol>=25k",
      [t for t in trades if t["score"]>=85 and t["entry"]<=0.55 and t["volume"]>=25000])

# Daily distribution for proposed tier
proposed = [t for t in trades if t["score"]>=85 and t["entry"]<=0.55 and t["volume"]>=50000]
print()
print("="*100)
print("Daily distribution: score>=85 + entry<=0.55 + vol>=50k")
print("="*100)
by_day = defaultdict(list)
for t in proposed: by_day[t["date"]].append(t)
for d in sorted(by_day):
    ts=by_day[d]; w=sum(1 for x in ts if x["win"])
    print(f"  {d}  N={len(ts):2d}  W={w:2d}  L={len(ts)-w:2d}")
print(f"\n  Days with signals: {len(by_day)}/{len(set(t['date'] for t in trades))}")
print(f"  Avg per day (when present): {len(proposed)/max(1,len(by_day)):.1f}")
print(f"  Avg per day (overall 16d):  {len(proposed)/16:.1f}")
