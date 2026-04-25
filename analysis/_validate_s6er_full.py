"""
Validate S6ER + recommended filter (S6ER-V) across the ENTIRE dataset
(Apr 10 - Apr 25, 2026), split by time periods to confirm stability.

Filters tested:
  Baseline S6ER:    score>=70 AND entry in [$0.30, $0.70]
  S6ER-V (rec):     S6ER + entry<=$0.50 + vol>=100k
  S6ER-V+:          S6ER-V + skip hour 14 UTC + skip mins_to_close >= 60
"""
import json, os
from datetime import datetime, timezone
from collections import defaultdict

SIGNAL_DIR = "logs/edge_signals"
MARKET_CACHE = os.path.join(SIGNAL_DIR, "_market_cache.json")
FEE = 0.02
RISK = 20.0


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# Load signals
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

# Build trade records
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
        win = True
        pnl = contracts * (1.0 - entry) - contracts * FEE
    else:
        win = False
        pnl = -contracts * entry - contracts * FEE
    cost = contracts * entry
    sig_ts = parse_ts(sig["ts"])
    ct_raw = sig.get("close_time") or mkt.get("close_time")
    mins_to_close = 9999
    if ct_raw:
        try:
            mins_to_close = (parse_ts(ct_raw) - sig_ts).total_seconds() / 60.0
        except Exception:
            pass
    det = sig.get("details") or {}
    trades.append({
        "ticker": tk,
        "ts": sig_ts,
        "date": sig["ts"][:10],
        "score": score,
        "entry": entry,
        "direction": direction,
        "best_bid_size": det.get("best_bid_size", 0),
        "volume": sig.get("volume") or 0,
        "mins_to_close": mins_to_close,
        "hour_utc": sig_ts.hour,
        "win": win,
        "pnl": pnl,
        "cost": cost,
    })

print(f"Settled S6ER trades total: {len(trades)}")
print(f"Date range: {min(t['date'] for t in trades)} -> {max(t['date'] for t in trades)}")
print()


def stats(trs, label):
    if not trs:
        return f"  {label:<30} N=  0"
    n = len(trs)
    w = sum(1 for t in trs if t["win"])
    net = sum(t["pnl"] for t in trs)
    cost = sum(t["cost"] for t in trs)
    wr = 100 * w / n
    roi = 100 * net / cost if cost else 0
    return f"  {label:<30} N={n:>4}  W/L={w:>3}/{n-w:<3}  WR={wr:5.1f}%  Net=${net:+8.2f}  ROI={roi:+6.1f}%"


def s6er_v(t):
    return t["entry"] <= 0.50 and t["volume"] >= 100_000


def s6er_v_plus(t):
    return s6er_v(t) and t["hour_utc"] != 14 and t["mins_to_close"] < 60


# ── Full dataset ──
print("=" * 95)
print("FULL DATASET (Apr 10 - Apr 25, 2026)")
print("=" * 95)
print(stats(trades, "Baseline S6ER"))
print(stats([t for t in trades if s6er_v(t)], "S6ER-V (entry<=.50 & v>=100k)"))
print(stats([t for t in trades if s6er_v_plus(t)], "S6ER-V+ (V + skip hr14 + <60m)"))
print()

# ── Split by week ──
def in_range(t, d_lo, d_hi):
    return d_lo <= t["date"] <= d_hi

periods = [
    ("Week 1: Apr 10-16", "2026-04-10", "2026-04-16"),
    ("Week 2: Apr 17-23", "2026-04-17", "2026-04-23"),
    ("Tail:   Apr 24-25", "2026-04-24", "2026-04-25"),
]
for name, lo, hi in periods:
    print("=" * 95)
    print(name)
    print("=" * 95)
    sub = [t for t in trades if in_range(t, lo, hi)]
    print(stats(sub, "Baseline S6ER"))
    print(stats([t for t in sub if s6er_v(t)], "S6ER-V"))
    print(stats([t for t in sub if s6er_v_plus(t)], "S6ER-V+"))
    print()

# ── Daily breakdown for the recommended filter ──
print("=" * 95)
print("S6ER-V (entry<=.50 & vol>=100k)  -  DAILY BREAKDOWN")
print("=" * 95)
print(f"  {'date':<12} {'N':>4}  {'W':>3} {'L':>3}  {'WR':>6}    {'Net':>9}    {'ROI':>7}")
by_day = defaultdict(list)
for t in trades:
    if s6er_v(t):
        by_day[t["date"]].append(t)
for d in sorted(by_day):
    trs = by_day[d]
    n = len(trs)
    w = sum(1 for t in trs if t["win"])
    net = sum(t["pnl"] for t in trs)
    cost = sum(t["cost"] for t in trs)
    wr = 100 * w / n
    roi = 100 * net / cost if cost else 0
    print(f"  {d}   {n:>3}  {w:>3} {n-w:>3}  {wr:5.1f}%   ${net:+8.2f}   {roi:+6.1f}%")

# ── Validation of the four claims ──
print()
print("=" * 95)
print("VALIDATION OF KEY CLAIMS (full dataset)")
print("=" * 95)
print(stats([t for t in trades if t["hour_utc"] == 14], "hour=14 UTC"))
print(stats([t for t in trades if t["mins_to_close"] >= 60], "mins_to_close >= 60"))
print(stats([t for t in trades if t["volume"] >= 100_000], "vol >= 100k (alone)"))
print(stats([t for t in trades if t["best_bid_size"] >= 2000 and t["volume"] >= 100_000],
            "bid_size>=2000 & vol>=100k"))
