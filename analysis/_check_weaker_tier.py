"""Simulate WEAKER positions (everything that does NOT qualify as premium tier).

Premium reference: score>=80 + entry<=0.55 + vol>=50k  (N=54, WR=96.3%, ROI=+112.7%)

Weaker = baseline S6ER MINUS premium. Then break down by sub-band to see
which slices are dragging down the average.
"""
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
        "score": score, "entry": entry, "volume": sig.get("volume") or 0,
        "win": win, "pnl": pnl, "cost": cost, "date": sig["ts"][:10],
    })

def is_premium(t):
    return t["score"] >= 80 and t["entry"] <= 0.55 and t["volume"] >= 50000

def stats(label, trs):
    if not trs:
        print(f"  {label:<60} N=   0"); return
    n=len(trs); w=sum(1 for t in trs if t["win"])
    net=sum(t["pnl"] for t in trs); cost=sum(t["cost"] for t in trs)
    print(f"  {label:<60} N={n:>4}  W/L={w:>3}/{n-w:<3}  WR={100*w/n:5.1f}%  Net=${net:+9.2f}  ROI={100*net/cost if cost else 0:+6.1f}%")

print(f"Total S6ER baseline trades: {len(trades)}")
print()
print("="*110)
print("TIER COMPARISON")
print("="*110)
stats("BASELINE (all S6ER)", trades)
stats("PREMIUM    (score>=80 + entry<=0.55 + vol>=50k)", [t for t in trades if is_premium(t)])
stats("WEAKER     (everything else)",                    [t for t in trades if not is_premium(t)])

# Decompose the weaker tier — which slice is worst?
weaker = [t for t in trades if not is_premium(t)]
print()
print("="*110)
print("WEAKER TIER — DECOMPOSITION (which sub-bands drag it down?)")
print("="*110)
print()
print("By score band:")
stats("  score 70-74", [t for t in weaker if 70 <= t["score"] < 75])
stats("  score 75-79", [t for t in weaker if 75 <= t["score"] < 80])
stats("  score 80-84", [t for t in weaker if 80 <= t["score"] < 85])
stats("  score 85-89", [t for t in weaker if 85 <= t["score"] < 90])
stats("  score 90+",   [t for t in weaker if t["score"] >= 90])

print()
print("By entry price band:")
stats("  entry 0.30-0.39", [t for t in weaker if 0.30 <= t["entry"] < 0.40])
stats("  entry 0.40-0.49", [t for t in weaker if 0.40 <= t["entry"] < 0.50])
stats("  entry 0.50-0.55", [t for t in weaker if 0.50 <= t["entry"] <= 0.55])
stats("  entry 0.56-0.60", [t for t in weaker if 0.55 <  t["entry"] <= 0.60])
stats("  entry 0.61-0.70", [t for t in weaker if 0.60 <  t["entry"] <= 0.70])

print()
print("By volume band:")
stats("  vol 5k-25k",   [t for t in weaker if 5000   <= t["volume"] < 25000])
stats("  vol 25k-50k",  [t for t in weaker if 25000  <= t["volume"] < 50000])
stats("  vol 50k-100k", [t for t in weaker if 50000  <= t["volume"] < 100000])
stats("  vol 100k+",    [t for t in weaker if 100000 <= t["volume"]])

print()
print("By WHY-not-premium reason (single failure):")
stats("  fails ONLY score (>=80 fail, entry<=0.55, vol>=50k)",
      [t for t in weaker if t["score"]<80 and t["entry"]<=0.55 and t["volume"]>=50000])
stats("  fails ONLY entry (score>=80, entry>0.55, vol>=50k)",
      [t for t in weaker if t["score"]>=80 and t["entry"]>0.55 and t["volume"]>=50000])
stats("  fails ONLY volume (score>=80, entry<=0.55, vol<50k)",
      [t for t in weaker if t["score"]>=80 and t["entry"]<=0.55 and t["volume"]<50000])
stats("  fails MULTIPLE",
      [t for t in weaker if not (
          (t["score"]<80 and t["entry"]<=0.55 and t["volume"]>=50000) or
          (t["score"]>=80 and t["entry"]>0.55 and t["volume"]>=50000) or
          (t["score"]>=80 and t["entry"]<=0.55 and t["volume"]<50000)
      )])

# Daily distribution of weaker tier
print()
print("="*110)
print("Daily breakdown — WEAKER TIER")
print("="*110)
by_day = defaultdict(list)
for t in weaker: by_day[t["date"]].append(t)
for d in sorted(by_day):
    ts=by_day[d]; w=sum(1 for x in ts if x["win"])
    net=sum(x["pnl"] for x in ts); cost=sum(x["cost"] for x in ts)
    roi = 100*net/cost if cost else 0
    print(f"  {d}  N={len(ts):3d}  W={w:3d}  L={len(ts)-w:3d}  WR={100*w/len(ts):5.1f}%  Net=${net:+8.2f}  ROI={roi:+6.1f}%")
