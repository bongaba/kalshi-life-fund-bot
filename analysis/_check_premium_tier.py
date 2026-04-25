"""Check WR/ROI for the proposed 'premium' tier: score>=85 AND entry<=0.55 AND vol>=50k."""
import json, glob, os, csv
from datetime import datetime, timezone
from collections import defaultdict

# Load market cache for settlement
CACHE = "logs/edge_signals/_market_cache.json"
with open(CACHE, "r", encoding="utf-8") as f:
    market_cache = json.load(f)

# Load all signal logs
signal_files = sorted(glob.glob("logs/edge_signals/edge_signals_*.jsonl"))

trades = []
for fp in signal_files:
    with open(fp, "r", encoding="utf-8") as f:
        for line in f:
            try:
                sig = json.loads(line)
            except Exception:
                continue
            if sig.get("event_type") != "edge_signal":
                continue

            ticker = sig.get("ticker")
            if not ticker:
                continue
            mkt = market_cache.get(ticker)
            if not mkt:
                continue
            status = mkt.get("status")
            if status not in ("finalized", "settled"):
                continue
            result = (mkt.get("result") or "").lower()
            if result not in ("yes", "no"):
                continue

            score = sig.get("score") or 0
            details = sig.get("details") or {}
            direction = sig.get("direction") or sig.get("side")
            if direction == "yes":
                entry = sig.get("yes_price")
            else:
                entry = sig.get("no_price")
            if entry is None:
                continue
            entry = float(entry)
            volume = sig.get("volume") or details.get("volume") or 0
            try:
                volume = float(volume)
            except Exception:
                volume = 0

            # S6ER baseline gate (current live config)
            if score < 70: continue
            if entry < 0.30 or entry > 0.70: continue
            imbalance = details.get("imbalance")
            if imbalance is not None and imbalance != 0:
                # current live uses imbalance=0 strict
                pass  # don't filter further -- match recent baseline
            if volume < 5000: continue

            # PnL
            risk = 20.0
            won = (direction == result)
            if won:
                pnl = risk * (1 - entry) / entry
            else:
                pnl = -risk

            ts = sig.get("timestamp") or sig.get("signal_ts_utc")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue

            trades.append({
                "score": score,
                "entry": entry,
                "volume": volume,
                "won": won,
                "pnl": pnl,
                "date": dt.date().isoformat(),
                "hour": dt.hour,
            })

def stats(label, ts):
    if not ts:
        print(f"  {label:<55} N=0")
        return
    n = len(ts)
    w = sum(1 for t in ts if t["won"])
    l = n - w
    net = sum(t["pnl"] for t in ts)
    risked = n * 20.0
    roi = 100 * net / risked if risked else 0
    wr = 100 * w / n
    print(f"  {label:<55} N={n:4d}  W/L={w:3d}/{l:3d}  WR={wr:5.1f}%  Net=${net:+9.2f}  ROI={roi:+6.1f}%")

print(f"\nTotal S6ER baseline trades: {len(trades)}")
print()
print("="*100)
print("Proposed PREMIUM TIER:  score>=85 AND entry<=0.55 AND vol>=50000")
print("="*100)

premium = [t for t in trades if t["score"] >= 85 and t["entry"] <= 0.55 and t["volume"] >= 50000]
standard_or_below = [t for t in trades if t not in premium]

stats("BASELINE S6ER (all trades)", trades)
stats("PREMIUM TIER (proposed)", premium)
stats("EVERYTHING ELSE", standard_or_below)

print()
print("="*100)
print("Tier breakdown variations to consider:")
print("="*100)
stats("score>=85 + entry<=0.55 + vol>=50k", premium)
stats("score>=85 + entry<=0.55 + vol>=100k", [t for t in trades if t["score"]>=85 and t["entry"]<=0.55 and t["volume"]>=100000])
stats("score>=85 + entry<=0.60 + vol>=50k",  [t for t in trades if t["score"]>=85 and t["entry"]<=0.60 and t["volume"]>=50000])
stats("score>=80 + entry<=0.55 + vol>=50k",  [t for t in trades if t["score"]>=80 and t["entry"]<=0.55 and t["volume"]>=50000])
stats("score>=90 + entry<=0.55 + vol>=50k",  [t for t in trades if t["score"]>=90 and t["entry"]<=0.55 and t["volume"]>=50000])

print()
print("="*100)
print("Daily count for premium tier (would I have enough signals?)")
print("="*100)
by_day = defaultdict(list)
for t in premium:
    by_day[t["date"]].append(t)
for d in sorted(by_day.keys()):
    ts = by_day[d]
    w = sum(1 for x in ts if x["won"])
    print(f"  {d}  N={len(ts):2d}  W={w:2d}  L={len(ts)-w:2d}")

avg_per_day = len(premium) / max(1, len(by_day))
print(f"\n  Avg premium signals per day: {avg_per_day:.1f}")
