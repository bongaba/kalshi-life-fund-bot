"""
Walk-forward validation:
  TRAIN window: Apr 10-17 (8 days)  -> derive best filter purely from this data
  TEST  window: Apr 18-25 (8 days)  -> apply derived filter to held-out data

If the filter still wins on TEST data it was never allowed to see, the edge is real.
"""
import json, os
from datetime import datetime
from collections import defaultdict
from itertools import combinations

SIGNAL_DIR = "logs/edge_signals"
MARKET_CACHE = os.path.join(SIGNAL_DIR, "_market_cache.json")
FEE = 0.02
RISK = 20.0
TRAIN_END = "2026-04-17"
TEST_START = "2026-04-18"


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
        "date": sig["ts"][:10],
        "score": score,
        "entry": entry,
        "direction": direction,
        "imbalance": det.get("imbalance", 0),
        "imbalance_pts": det.get("imbalance_pts", 0),
        "spread_cents": det.get("spread_cents", 0),
        "best_bid_size": det.get("best_bid_size", 0),
        "volume": sig.get("volume") or 0,
        "mins_to_close": mins_to_close,
        "hour_utc": sig_ts.hour,
        "win": win,
        "pnl": pnl,
        "cost": cost,
    })

train = [t for t in trades if t["date"] <= TRAIN_END]
test = [t for t in trades if t["date"] >= TEST_START]
print(f"Total trades: {len(trades)}")
print(f"  TRAIN (<= {TRAIN_END}): {len(train)}")
print(f"  TEST  (>= {TEST_START}): {len(test)}")
print()


def stats_of(trs):
    if not trs:
        return (0, 0, 0, 0, 0.0, 0.0)
    n = len(trs)
    w = sum(1 for t in trs if t["win"])
    net = sum(t["pnl"] for t in trs)
    cost = sum(t["cost"] for t in trs)
    wr = 100 * w / n
    roi = 100 * net / cost if cost else 0.0
    return (n, w, n - w, net, wr, roi)


def fmt(label, trs):
    n, w, l, net, wr, roi = stats_of(trs)
    return f"  {label:<45}  N={n:>4}  W/L={w:>3}/{l:<3}  WR={wr:5.1f}%  Net=${net:+8.2f}  ROI={roi:+6.1f}%"


# ── Define candidate filters (single-feature thresholds) ──
def filter_candidates():
    feats = []
    for thr in (0.40, 0.45, 0.50, 0.55, 0.60):
        feats.append((f"entry<={thr:.2f}", lambda t, x=thr: t["entry"] <= x))
    for thr in (10_000, 50_000, 100_000, 200_000):
        feats.append((f"vol>={thr//1000}k", lambda t, x=thr: t["volume"] >= x))
    for thr in (0.70, 0.80, 0.85, 0.90, 0.95):
        feats.append((f"imbalance>={thr:.2f}", lambda t, x=thr: t["imbalance"] >= x))
    for thr in (10, 20, 30):
        feats.append((f"imb_pts>={thr}", lambda t, x=thr: t["imbalance_pts"] >= x))
    feats.append(("spread=0", lambda t: t["spread_cents"] == 0))
    feats.append(("spread<=2c", lambda t: t["spread_cents"] <= 2))
    for thr in (75, 80, 85, 90):
        feats.append((f"score>={thr}", lambda t, x=thr: t["score"] >= x))
    for thr in (15, 30, 60):
        feats.append((f"close<{thr}m", lambda t, x=thr: t["mins_to_close"] < x))
    feats.append(("dir=YES", lambda t: t["direction"] == "YES"))
    feats.append(("dir=NO", lambda t: t["direction"] == "NO"))
    feats.append(("bid_size>=2000", lambda t: t["best_bid_size"] >= 2000))
    return feats


cands = filter_candidates()


# ── Search: best 2-feature AND combo on TRAIN, then evaluate on TEST ──
print("=" * 100)
print("STEP 1: derive best 2-feature filter from TRAIN window only")
print("=" * 100)
MIN_N_TRAIN = 12
results = []
for (n1, f1), (n2, f2) in combinations(cands, 2):
    sub = [t for t in train if f1(t) and f2(t)]
    if len(sub) < MIN_N_TRAIN:
        continue
    n, w, l, net, wr, roi = stats_of(sub)
    results.append((roi, wr, n, f"{n1} & {n2}", f1, f2))
results.sort(key=lambda r: -r[0])

print(f"  Top 10 filters by TRAIN ROI (min N={MIN_N_TRAIN}):")
for roi, wr, n, name, _, _ in results[:10]:
    print(f"    {name:<55}  N={n:>3}  WR={wr:5.1f}%  ROI={roi:+6.1f}%")
print()


# ── STEP 2: apply each top-10 TRAIN winner to TEST data ──
print("=" * 100)
print("STEP 2: apply each TRAIN-derived filter to held-out TEST data")
print("=" * 100)
print(f"  {'filter':<55}  {'TRAIN':>22}  {'TEST (out-of-sample)':>30}")
print(f"  {'-'*55}  {'-'*22}  {'-'*30}")
for roi, wr, n, name, f1, f2 in results[:10]:
    tr_sub = [t for t in train if f1(t) and f2(t)]
    te_sub = [t for t in test if f1(t) and f2(t)]
    tn, _, _, _, twr, troi = stats_of(tr_sub)
    en, ew, el, enet, ewr, eroi = stats_of(te_sub)
    print(f"  {name:<55}  N={tn:>3} WR={twr:4.1f}% ROI={troi:+6.1f}%  "
          f"N={en:>3} W/L={ew}/{el} WR={ewr:5.1f}% ROI={eroi:+6.1f}%")
print()


# ── STEP 3: focus on the SPECIFIC proposed filter ──
print("=" * 100)
print("STEP 3: validate the EXACT proposed filter on held-out TEST data")
print("=" * 100)


def s6er_v(t):
    return t["entry"] <= 0.50 and t["volume"] >= 100_000


def s6er_v_plus(t):
    return s6er_v(t) and t["hour_utc"] != 14 and t["mins_to_close"] < 60


print(fmt("TRAIN  Baseline S6ER", train))
print(fmt("TRAIN  S6ER-V (entry<=.50 & vol>=100k)", [t for t in train if s6er_v(t)]))
print(fmt("TRAIN  S6ER-V+ (V + skip hr14 + <60m)", [t for t in train if s6er_v_plus(t)]))
print()
print(fmt("TEST   Baseline S6ER (held-out)", test))
print(fmt("TEST   S6ER-V (held-out)", [t for t in test if s6er_v(t)]))
print(fmt("TEST   S6ER-V+ (held-out)", [t for t in test if s6er_v_plus(t)]))
