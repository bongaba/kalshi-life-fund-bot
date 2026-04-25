"""
S6ER deep-dive: find sub-filters within S6ER that produce the highest-certainty wins.

Loads all S6ER-qualifying signals (score>=70, entry $0.30-$0.70), joins with
cached settlement results, and slices by every available dimension to rank
sub-criteria by win rate and ROI.

Dimensions analyzed:
  - score buckets           (70-74, 75-79, 80-84, 85-89, 90+)
  - entry price buckets
  - direction               (YES vs NO)
  - imbalance_pts           (0, 5, 10, 15, 20, 25+)
  - spread_cents
  - spread_pts
  - top_pts                 (best-bid-size depth score)
  - flow_pts                (30s order flow score)
  - best_bid_size           (liquidity depth)
  - volume                  (market volume)
  - minutes to close
  - hour of day (UTC)

Then searches for AND-combinations of filters that beat naked S6ER.

Output: simulate_s6er_deepdive.txt
"""
import json
import os
import math
from collections import defaultdict
from datetime import datetime, timezone

SIGNAL_DIR = "logs/edge_signals"
MARKET_CACHE = os.path.join(SIGNAL_DIR, "_market_cache.json")
OUT_FILE = "simulate_s6er_deepdive.txt"

FEE_PER_CONTRACT = 0.02
MIN_SCORE = 70.0
ENTRY_MIN = 0.30
ENTRY_MAX = 0.70
BANKROLL = 1000.00


def parse_ts(ts_str):
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


# ── Load signals, dedupe by ticker (last signal wins) ──
print("Loading signals...", flush=True)
raw = []
for fname in sorted(os.listdir(SIGNAL_DIR)):
    if not fname.startswith("edge_signals_") or not fname.endswith(".jsonl"):
        continue
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
print(f"  {len(raw)} raw signals, {len(by_ticker)} unique tickers", flush=True)


# ── Load cached market settlements ──
print("Loading cached settlements...", flush=True)
with open(MARKET_CACHE) as f:
    cache = json.load(f)
print(f"  {len(cache)} markets cached", flush=True)


# ── Build settled S6ER trade records with rich features ──
trades = []
for tk, sig in by_ticker.items():
    score = sig.get("score") or 0
    if score < MIN_SCORE:
        continue
    d = sig.get("direction")
    entry = sig.get("yes_price") if d == "YES" else sig.get("no_price")
    if entry is None or entry < ENTRY_MIN or entry > ENTRY_MAX:
        continue
    mkt = cache.get(tk)
    if not mkt:
        continue
    status = mkt.get("status", "")
    result = mkt.get("result", "")
    if status not in ("finalized", "settled"):
        continue
    won = (d == "YES" and result == "yes") or (d == "NO" and result == "no")

    det = sig.get("details") or {}
    sig_ts = parse_ts(sig["ts"])
    ct_raw = sig.get("close_time") or mkt.get("close_time")
    mins_to_close = None
    if ct_raw:
        try:
            mins_to_close = (parse_ts(ct_raw) - sig_ts).total_seconds() / 60.0
        except Exception:
            pass

    trades.append({
        "ticker": tk,
        "direction": d,
        "score": score,
        "entry": entry,
        "won": won,
        "ts": sig["ts"],
        "hour_utc": sig_ts.hour,
        "mins_to_close": mins_to_close,
        "imbalance_pts": det.get("imbalance_pts", 0),
        "imbalance": det.get("imbalance", 0),
        "spread_cents": det.get("spread_cents", 0),
        "spread_pts": det.get("spread_pts", 0),
        "top_pts": det.get("top_pts", 0),
        "flow_pts": det.get("flow_pts", 0),
        "best_bid_size": det.get("best_bid_size", 0),
        "our_depth": det.get("our_depth", 0),
        "opp_depth": det.get("opp_depth", 0),
        "volume": sig.get("volume", 0),
    })

print(f"Settled S6ER trades: {len(trades)}", flush=True)


# ── ROI/WR helpers ──
def stats(ts):
    n = len(ts)
    if n == 0:
        return None
    wins = sum(1 for t in ts if t["won"])
    losses = n - wins
    # Per-trade even-split ROI against $1000 notional (apples-to-apples slice compare)
    per_trade = BANKROLL / n
    gross = 0.0
    fees = 0.0
    for t in ts:
        if t["entry"] <= 0:
            continue
        c = math.floor(per_trade / t["entry"])
        if c <= 0:
            continue
        f = c * FEE_PER_CONTRACT
        fees += f
        if t["won"]:
            gross += c * (1.0 - t["entry"])
        else:
            gross += -(c * t["entry"])
    net = gross - fees
    roi = net / BANKROLL * 100.0
    return {
        "n": n, "w": wins, "l": losses,
        "wr": wins / n * 100.0,
        "net": net, "roi": roi,
    }


def slice_by(label, bucket_fn, trades, out):
    buckets = defaultdict(list)
    for t in trades:
        key = bucket_fn(t)
        if key is None:
            continue
        buckets[key].append(t)
    out.append("")
    out.append("-" * 110)
    out.append(f"  SLICE: {label}")
    out.append("-" * 110)
    out.append(f"  {'bucket':<30} {'N':>5} {'W':>4} {'L':>4} {'WR%':>6} "
               f"{'Net':>10} {'ROI%':>7}")
    rows = []
    for k, ts in buckets.items():
        s = stats(ts)
        if s and s["n"] >= 5:
            rows.append((k, s))
    # sort by WR desc for readability, ties by N desc
    rows.sort(key=lambda x: (x[1]["wr"], x[1]["n"]), reverse=True)
    for k, s in rows:
        out.append(f"  {str(k):<30} {s['n']:>5d} {s['w']:>4d} {s['l']:>4d} "
                   f"{s['wr']:>5.1f}% ${s['net']:>+7.2f} {s['roi']:>+6.1f}%")


lines = []
lines.append("=" * 110)
lines.append("  S6ER DEEP-DIVE — finding highest-certainty sub-criteria")
lines.append("=" * 110)
lines.append(f"  Generated: {datetime.now().isoformat(timespec='seconds')}")
lines.append(f"  Base filter: score>=70 AND entry in [${ENTRY_MIN}, ${ENTRY_MAX}]")
lines.append(f"  Settled S6ER trades in sample: {len(trades)}")
baseline = stats(trades)
if baseline:
    lines.append(f"  Baseline S6ER:  N={baseline['n']}  W/L={baseline['w']}/{baseline['l']}  "
                 f"WR={baseline['wr']:.1f}%  NET=${baseline['net']:+.2f}  ROI={baseline['roi']:+.1f}%")
lines.append("")
lines.append("  (Buckets with N<5 suppressed. Rows sorted by WR%.)")


# ── Single-dimension slices ──
slice_by("SCORE BUCKETS",
         lambda t: "70-74" if t["score"] < 75 else "75-79" if t["score"] < 80
                   else "80-84" if t["score"] < 85 else "85-89" if t["score"] < 90 else "90+",
         trades, lines)

slice_by("ENTRY PRICE BUCKETS",
         lambda t: "$0.30-0.39" if t["entry"] < 0.40
                   else "$0.40-0.49" if t["entry"] < 0.50
                   else "$0.50-0.59" if t["entry"] < 0.60
                   else "$0.60-0.70",
         trades, lines)

slice_by("DIRECTION",
         lambda t: t["direction"],
         trades, lines)

slice_by("IMBALANCE_PTS",
         lambda t: "0" if t["imbalance_pts"] == 0
                   else "1-9" if t["imbalance_pts"] < 10
                   else "10-19" if t["imbalance_pts"] < 20
                   else "20-29" if t["imbalance_pts"] < 30
                   else "30+",
         trades, lines)

slice_by("IMBALANCE RATIO",
         lambda t: "<0.50" if t["imbalance"] < 0.50
                   else "0.50-0.69" if t["imbalance"] < 0.70
                   else "0.70-0.84" if t["imbalance"] < 0.85
                   else "0.85-0.94" if t["imbalance"] < 0.95
                   else "0.95+",
         trades, lines)

slice_by("SPREAD_CENTS",
         lambda t: "0" if t["spread_cents"] == 0
                   else "1-2" if t["spread_cents"] <= 2
                   else "3-5" if t["spread_cents"] <= 5
                   else "6+",
         trades, lines)

slice_by("TOP_PTS (best-bid size)",
         lambda t: "0-9" if t["top_pts"] < 10
                   else "10-14" if t["top_pts"] < 15
                   else "15-19" if t["top_pts"] < 20
                   else "20+",
         trades, lines)

slice_by("FLOW_PTS (30s order flow)",
         lambda t: "0" if t["flow_pts"] == 0
                   else "1-9" if t["flow_pts"] < 10
                   else "10-19" if t["flow_pts"] < 20
                   else "20+",
         trades, lines)

slice_by("BEST_BID_SIZE (liquidity)",
         lambda t: "<100" if t["best_bid_size"] < 100
                   else "100-499" if t["best_bid_size"] < 500
                   else "500-1999" if t["best_bid_size"] < 2000
                   else "2000+",
         trades, lines)

slice_by("MINUTES TO CLOSE",
         lambda t: None if t["mins_to_close"] is None
                   else "<5" if t["mins_to_close"] < 5
                   else "5-14" if t["mins_to_close"] < 15
                   else "15-29" if t["mins_to_close"] < 30
                   else "30-59" if t["mins_to_close"] < 60
                   else "60+",
         trades, lines)

slice_by("HOUR OF DAY (UTC)",
         lambda t: f"{t['hour_utc']:02d}",
         trades, lines)

slice_by("VOLUME",
         lambda t: "<10k" if t["volume"] < 10_000
                   else "10k-50k" if t["volume"] < 50_000
                   else "50k-100k" if t["volume"] < 100_000
                   else "100k+",
         trades, lines)


# ── AND-combination search: find top 2-feature filters beating baseline ──
lines.append("")
lines.append("=" * 110)
lines.append("  TOP 2-FEATURE AND-COMBINATIONS (ranked by ROI, min N=15)")
lines.append("=" * 110)

feature_filters = [
    ("score>=75",            lambda t: t["score"] >= 75),
    ("score>=80",            lambda t: t["score"] >= 80),
    ("score>=85",            lambda t: t["score"] >= 85),
    ("entry<=0.50",          lambda t: t["entry"] <= 0.50),
    ("entry<=0.45",          lambda t: t["entry"] <= 0.45),
    ("entry<=0.40",          lambda t: t["entry"] <= 0.40),
    ("entry>=0.40",          lambda t: t["entry"] >= 0.40),
    ("entry>=0.50",          lambda t: t["entry"] >= 0.50),
    ("dir=YES",              lambda t: t["direction"] == "YES"),
    ("dir=NO",               lambda t: t["direction"] == "NO"),
    ("imbalance>=0.70",      lambda t: t["imbalance"] >= 0.70),
    ("imbalance>=0.85",      lambda t: t["imbalance"] >= 0.85),
    ("imbalance_pts>=10",    lambda t: t["imbalance_pts"] >= 10),
    ("imbalance_pts>=20",    lambda t: t["imbalance_pts"] >= 20),
    ("spread<=2c",           lambda t: t["spread_cents"] <= 2),
    ("spread=0",             lambda t: t["spread_cents"] == 0),
    ("top_pts>=15",          lambda t: t["top_pts"] >= 15),
    ("top_pts>=20",          lambda t: t["top_pts"] >= 20),
    ("flow_pts>=10",         lambda t: t["flow_pts"] >= 10),
    ("flow_pts>=20",         lambda t: t["flow_pts"] >= 20),
    ("bid_size>=500",        lambda t: t["best_bid_size"] >= 500),
    ("bid_size>=2000",       lambda t: t["best_bid_size"] >= 2000),
    ("close>=30m",           lambda t: (t["mins_to_close"] or 0) >= 30),
    ("close>=60m",           lambda t: (t["mins_to_close"] or 0) >= 60),
    ("close<30m",            lambda t: (t["mins_to_close"] is not None) and t["mins_to_close"] < 30),
    ("vol>=50k",             lambda t: t["volume"] >= 50_000),
    ("vol>=100k",            lambda t: t["volume"] >= 100_000),
]

combos = []
# Single-feature first
for name, fn in feature_filters:
    sub = [t for t in trades if fn(t)]
    s = stats(sub)
    if s and s["n"] >= 15:
        combos.append((name, s))

# 2-feature AND
for i, (n1, f1) in enumerate(feature_filters):
    for j, (n2, f2) in enumerate(feature_filters):
        if j <= i:
            continue
        sub = [t for t in trades if f1(t) and f2(t)]
        s = stats(sub)
        if s and s["n"] >= 15:
            combos.append((f"{n1} & {n2}", s))

combos.sort(key=lambda x: x[1]["roi"], reverse=True)

lines.append(f"  {'filter':<45} {'N':>5} {'W':>4} {'L':>4} {'WR%':>6} "
             f"{'Net':>10} {'ROI%':>7}")
for name, s in combos[:40]:
    lines.append(f"  {name:<45} {s['n']:>5d} {s['w']:>4d} {s['l']:>4d} "
                 f"{s['wr']:>5.1f}% ${s['net']:>+7.2f} {s['roi']:>+6.1f}%")

lines.append("")
lines.append("=" * 110)
lines.append("  TOP BY WIN-RATE (ranked by WR%, min N=20 to be meaningful)")
lines.append("=" * 110)
combos_wr = sorted((c for c in combos if c[1]["n"] >= 20),
                   key=lambda x: (x[1]["wr"], x[1]["roi"]), reverse=True)
lines.append(f"  {'filter':<45} {'N':>5} {'W':>4} {'L':>4} {'WR%':>6} "
             f"{'Net':>10} {'ROI%':>7}")
for name, s in combos_wr[:25]:
    lines.append(f"  {name:<45} {s['n']:>5d} {s['w']:>4d} {s['l']:>4d} "
                 f"{s['wr']:>5.1f}% ${s['net']:>+7.2f} {s['roi']:>+6.1f}%")

lines.append("")
lines.append("=" * 110)

with open(OUT_FILE, "w") as f:
    f.write("\n".join(lines))

print(f"Wrote {OUT_FILE}")
if baseline:
    print(f"Baseline S6ER: N={baseline['n']} WR={baseline['wr']:.1f}% ROI={baseline['roi']:+.1f}%")
print("Top 5 by ROI:")
for name, s in combos[:5]:
    print(f"  {name:<45} N={s['n']:>3d}  WR={s['wr']:>5.1f}%  ROI={s['roi']:+.1f}%")
print("Top 5 by WR (min N=20):")
for name, s in combos_wr[:5]:
    print(f"  {name:<45} N={s['n']:>3d}  WR={s['wr']:>5.1f}%  ROI={s['roi']:+.1f}%")
