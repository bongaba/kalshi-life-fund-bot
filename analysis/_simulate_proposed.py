"""
Simulate variant: vol>=100k + skip hour 14 + skip mins_to_close>=60
(Does NOT lower entry_max — keeps current $0.30-$0.70 entry range)
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
                    try:
                        raw.append(json.loads(line))
                    except Exception:
                        pass

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
        try:
            mins_to_close = (parse_ts(ct_raw) - sig_ts).total_seconds() / 60.0
        except Exception:
            pass
    trades.append({
        "date": sig["ts"][:10],
        "entry": entry,
        "volume": sig.get("volume") or 0,
        "mins_to_close": mins_to_close,
        "hour_utc": sig_ts.hour,
        "win": win, "pnl": pnl, "cost": cost,
    })


def stats(trs, label):
    if not trs:
        return f"  {label:<55} N=  0"
    n = len(trs)
    w = sum(1 for t in trs if t["win"])
    net = sum(t["pnl"] for t in trs)
    cost = sum(t["cost"] for t in trs)
    return (f"  {label:<55} N={n:>4}  W/L={w:>3}/{n-w:<3}  WR={100*w/n:5.1f}%  "
            f"Net=${net:+8.2f}  ROI={100*net/cost if cost else 0:+6.1f}%")


# Variant filters
def variant(t):
    """Proposed: vol>=100k AND skip hr14 AND mins<60"""
    return (t["volume"] >= 100_000
            and t["hour_utc"] != 14
            and t["mins_to_close"] < 60)


def variant_alt(t):
    """For comparison: also cap entry<=0.50"""
    return variant(t) and t["entry"] <= 0.50


print("=" * 100)
print(f"FULL DATASET ({min(t['date'] for t in trades)} -> {max(t['date'] for t in trades)})")
print("=" * 100)
print(stats(trades, "Baseline S6ER (current live config)"))
print(stats([t for t in trades if t["volume"] >= 100_000],
            "vol>=100k only"))
print(stats([t for t in trades if variant(t)],
            "PROPOSED: vol>=100k + skip hr14 + close<60m"))
print(stats([t for t in trades if variant_alt(t)],
            "(for comparison) + entry<=0.50"))
print()

# Time splits
periods = [
    ("Week 1: Apr 10-16", "2026-04-10", "2026-04-16"),
    ("Week 2: Apr 17-23", "2026-04-17", "2026-04-23"),
    ("Tail:   Apr 24-25", "2026-04-24", "2026-04-25"),
]
for name, lo, hi in periods:
    print("=" * 100)
    print(name)
    print("=" * 100)
    sub = [t for t in trades if lo <= t["date"] <= hi]
    print(stats(sub, "Baseline S6ER"))
    print(stats([t for t in sub if variant(t)], "PROPOSED filter"))
    print(stats([t for t in sub if variant_alt(t)], "(+ entry<=0.50)"))
    print()

# Daily breakdown of proposed
print("=" * 100)
print("PROPOSED FILTER  -  DAILY BREAKDOWN")
print("=" * 100)
print(f"  {'date':<12} {'N':>4}  {'W':>3} {'L':>3}  {'WR':>6}    {'Net':>9}    {'ROI':>7}")
by_day = defaultdict(list)
for t in trades:
    if variant(t):
        by_day[t["date"]].append(t)
totN = totW = 0
totNet = totCost = 0.0
for d in sorted(by_day):
    trs = by_day[d]
    n = len(trs); w = sum(1 for t in trs if t["win"])
    net = sum(t["pnl"] for t in trs); cost = sum(t["cost"] for t in trs)
    totN += n; totW += w; totNet += net; totCost += cost
    print(f"  {d}   {n:>3}  {w:>3} {n-w:>3}  {100*w/n:5.1f}%   ${net:+8.2f}   {100*net/cost:+6.1f}%")
print(f"  {'TOTAL':<12}  {totN:>3}  {totW:>3} {totN-totW:>3}  {100*totW/totN:5.1f}%   ${totNet:+8.2f}   {100*totNet/totCost:+6.1f}%")

# Walk-forward sanity check on proposed
print()
print("=" * 100)
print("WALK-FORWARD: PROPOSED filter applied to held-out test windows")
print("=" * 100)
folds = [
    ("Fold 1  TEST 04-15..04-17", "2026-04-15", "2026-04-17"),
    ("Fold 2  TEST 04-20..04-22", "2026-04-20", "2026-04-22"),
    ("Fold 3  TEST 04-23..04-25", "2026-04-23", "2026-04-25"),
]
for name, lo, hi in folds:
    sub = [t for t in trades if lo <= t["date"] <= hi and variant(t)]
    print(stats(sub, name))
