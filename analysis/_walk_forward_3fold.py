"""3-fold rolling walk-forward validation of S6ER-V."""
import json, os
from datetime import datetime
from itertools import combinations

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
    det = sig.get("details") or {}
    trades.append({
        "date": sig["ts"][:10],
        "score": score, "entry": entry, "direction": direction,
        "imbalance": det.get("imbalance", 0),
        "imbalance_pts": det.get("imbalance_pts", 0),
        "spread_cents": det.get("spread_cents", 0),
        "best_bid_size": det.get("best_bid_size", 0),
        "volume": sig.get("volume") or 0,
        "mins_to_close": mins_to_close,
        "hour_utc": sig_ts.hour,
        "win": win, "pnl": pnl, "cost": cost,
    })


def stats_of(trs):
    if not trs:
        return (0, 0, 0, 0.0, 0.0, 0.0)
    n = len(trs)
    w = sum(1 for t in trs if t["win"])
    net = sum(t["pnl"] for t in trs)
    cost = sum(t["cost"] for t in trs)
    return (n, w, n - w, net, 100*w/n, 100*net/cost if cost else 0.0)


def filter_candidates():
    feats = []
    for thr in (0.40, 0.45, 0.50, 0.55, 0.60):
        feats.append((f"entry<={thr:.2f}", lambda t, x=thr: t["entry"] <= x))
    for thr in (10_000, 50_000, 100_000, 200_000):
        feats.append((f"vol>={thr//1000}k", lambda t, x=thr: t["volume"] >= x))
    for thr in (0.70, 0.80, 0.85, 0.90, 0.95):
        feats.append((f"imb>={thr:.2f}", lambda t, x=thr: t["imbalance"] >= x))
    for thr in (10, 20, 30):
        feats.append((f"imb_pts>={thr}", lambda t, x=thr: t["imbalance_pts"] >= x))
    feats.append(("spread=0", lambda t: t["spread_cents"] == 0))
    for thr in (75, 80, 85, 90):
        feats.append((f"score>={thr}", lambda t, x=thr: t["score"] >= x))
    for thr in (15, 30, 60):
        feats.append((f"close<{thr}m", lambda t, x=thr: t["mins_to_close"] < x))
    feats.append(("dir=YES", lambda t: t["direction"] == "YES"))
    feats.append(("dir=NO", lambda t: t["direction"] == "NO"))
    feats.append(("bid>=2000", lambda t: t["best_bid_size"] >= 2000))
    return feats


def s6er_v(t):
    return t["entry"] <= 0.50 and t["volume"] >= 100_000


cands = filter_candidates()


def run_fold(name, train_lo, train_hi, test_lo, test_hi):
    print("=" * 100)
    print(f"FOLD {name}:  TRAIN {train_lo}..{train_hi}   TEST {test_lo}..{test_hi}")
    print("=" * 100)
    train = [t for t in trades if train_lo <= t["date"] <= train_hi]
    test = [t for t in trades if test_lo <= t["date"] <= test_hi]
    print(f"  train trades: {len(train)}   test trades: {len(test)}")

    # Find best 2-feature combo on TRAIN
    results = []
    for (n1, f1), (n2, f2) in combinations(cands, 2):
        sub = [t for t in train if f1(t) and f2(t)]
        if len(sub) < 10:
            continue
        n, w, l, net, wr, roi = stats_of(sub)
        results.append((roi, name, n, f"{n1} & {n2}", f1, f2))
    results.sort(key=lambda r: -r[0])

    print(f"  best 5 TRAIN filters (min N=10):")
    for roi, _, n, lbl, f1, f2 in results[:5]:
        tn, _, _, _, twr, troi = stats_of([t for t in train if f1(t) and f2(t)])
        en, ew, el, _, ewr, eroi = stats_of([t for t in test if f1(t) and f2(t)])
        print(f"    {lbl:<45}  TRAIN N={tn:>3} WR={twr:5.1f}% ROI={troi:+6.1f}%  | TEST N={en:>3} W/L={ew}/{el} WR={ewr:5.1f}% ROI={eroi:+6.1f}%")

    # Specific recommended filter
    sub = [t for t in test if s6er_v(t)]
    n, w, l, net, wr, roi = stats_of(sub)
    print(f"\n  RECOMMENDED FILTER on TEST (entry<=.50 & vol>=100k):")
    print(f"    N={n}  W/L={w}/{l}  WR={wr:.1f}%  Net=${net:+.2f}  ROI={roi:+.1f}%")
    print()
    return (n, wr, roi)


# 3 rolling folds
folds = [
    ("1", "2026-04-10", "2026-04-14", "2026-04-15", "2026-04-17"),
    ("2", "2026-04-10", "2026-04-19", "2026-04-20", "2026-04-22"),
    ("3", "2026-04-10", "2026-04-22", "2026-04-23", "2026-04-25"),
]
results = []
for f in folds:
    results.append(run_fold(*f))

print("=" * 100)
print("SUMMARY: recommended filter (entry<=0.50 & vol>=100k) on each held-out TEST window")
print("=" * 100)
for (name, _, _, lo, hi), (n, wr, roi) in zip(folds, results):
    print(f"  Fold {name}  TEST {lo}..{hi}   N={n:>3}  WR={wr:5.1f}%  ROI={roi:+6.1f}%")
