"""Premium tier variations + mins_to_close < 60 guard."""
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
    sig_ts = parse_ts(sig["ts"])
    ct_raw = sig.get("close_time") or mkt.get("close_time")
    mins_to_close = 9999
    if ct_raw:
        try: mins_to_close = (parse_ts(ct_raw) - sig_ts).total_seconds() / 60.0
        except Exception: pass
    trades.append({
        "date": sig["ts"][:10],
        "score": score,
        "entry": entry,
        "volume": sig.get("volume") or 0,
        "mins_to_close": mins_to_close,
        "hour": sig_ts.hour,
        "win": win, "pnl": pnl, "cost": cost,
    })

def stats(label, trs):
    if not trs:
        print(f"  {label:<60} N=   0"); return
    n=len(trs); w=sum(1 for t in trs if t["win"])
    net=sum(t["pnl"] for t in trs); cost=sum(t["cost"] for t in trs)
    print(f"  {label:<60} N={n:>4}  W/L={w:>3}/{n-w:<3}  WR={100*w/n:5.1f}%  Net=${net:+9.2f}  ROI={100*net/cost if cost else 0:+6.1f}%")

def filt(score_min, entry_max, vol_min, with_guard):
    out = []
    for t in trades:
        if t["score"] < score_min: continue
        if t["entry"] > entry_max: continue
        if t["volume"] < vol_min: continue
        if with_guard and t["mins_to_close"] >= 60: continue
        out.append(t)
    return out

print(f"Total S6ER baseline trades: {len(trades)}")
print()
print("="*110)
print("PREMIUM TIER WITH mins_to_close < 60 GUARD")
print("="*110)
stats("BASELINE S6ER (all)", trades)
print()
print("  -- without guard --                                                     vs  -- with guard --")

variants = [
    ("score>=85 + entry<=0.55 + vol>=50k",  85, 0.55, 50000),
    ("score>=85 + entry<=0.55 + vol>=100k", 85, 0.55, 100000),
    ("score>=85 + entry<=0.60 + vol>=50k",  85, 0.60, 50000),
    ("score>=80 + entry<=0.55 + vol>=50k",  80, 0.55, 50000),
    ("score>=90 + entry<=0.55 + vol>=50k",  90, 0.55, 50000),
    ("score>=85 + entry<=0.50 + vol>=50k",  85, 0.50, 50000),
]
for label, smin, emax, vmin in variants:
    print()
    stats(f"{label}  (no guard)", filt(smin, emax, vmin, False))
    stats(f"{label}  + mins<60",  filt(smin, emax, vmin, True))

# Daily breakdown for top candidate
print()
print("="*110)
print("Daily distribution: score>=80 + entry<=0.55 + vol>=50k + mins<60")
print("="*110)
top = filt(80, 0.55, 50000, True)
by_day = defaultdict(list)
for t in top: by_day[t["date"]].append(t)
for d in sorted(by_day):
    ts=by_day[d]; w=sum(1 for x in ts if x["win"])
    print(f"  {d}  N={len(ts):2d}  W={w:2d}  L={len(ts)-w:2d}")
print(f"\n  Days with signals: {len(by_day)}/16")
print(f"  Avg per day (overall 16d):  {len(top)/16:.1f}")
