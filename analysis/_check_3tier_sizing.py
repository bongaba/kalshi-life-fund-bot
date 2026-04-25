"""Simulate the 3-tier sizing system from the original proposal:

PREMIUM  $35: score>=85 AND entry<=0.55 AND vol>=50k
STANDARD $20: score>=75 AND entry<=0.65   (and not premium)
PROBE    $12: everything else passing S6ER baseline

S6ER baseline (current live): score>=70, entry 0.30-0.70, vol>=5k
"""
import json, os
from collections import defaultdict

SIGNAL_DIR = "logs/edge_signals"
MARKET_CACHE = os.path.join(SIGNAL_DIR, "_market_cache.json")
FEE = 0.02

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

def classify(score, entry, vol):
    if score >= 85 and entry <= 0.55 and vol >= 50000:
        return "PREMIUM", 35.0
    if score >= 75 and entry <= 0.65:
        return "STANDARD", 20.0
    return "PROBE", 12.0

trades_by_tier = defaultdict(list)
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
    vol = sig.get("volume") or 0
    tier, risk = classify(score, entry, vol)
    contracts = max(1, int(risk / entry))
    if (direction == "YES" and settle == "yes") or (direction == "NO" and settle == "no"):
        win = True
        pnl = contracts * (1.0 - entry) - contracts * FEE
    else:
        win = False
        pnl = -contracts * entry - contracts * FEE
    cost = contracts * entry
    trades_by_tier[tier].append({
        "score": score, "entry": entry, "vol": vol,
        "win": win, "pnl": pnl, "cost": cost, "date": sig["ts"][:10],
        "contracts": contracts, "risk": risk,
    })

def stats(label, trs):
    if not trs:
        print(f"  {label:<55} N=   0"); return
    n=len(trs); w=sum(1 for t in trs if t["win"])
    net=sum(t["pnl"] for t in trs); cost=sum(t["cost"] for t in trs)
    avg_contracts = sum(t["contracts"] for t in trs)/n
    print(f"  {label:<55} N={n:>4}  W/L={w:>3}/{n-w:<3}  WR={100*w/n:5.1f}%  Net=${net:+9.2f}  ROI={100*net/cost if cost else 0:+6.1f}%  avg_ct={avg_contracts:5.1f}")

all_trades = sum(trades_by_tier.values(), [])
print(f"Total S6ER baseline trades: {len(all_trades)}")
print()
print("="*120)
print("PROPOSED 3-TIER SIZING")
print("  PREMIUM  $35  =  score>=85 AND entry<=0.55 AND vol>=50k")
print("  STANDARD $20  =  score>=75 AND entry<=0.65   (and not premium)")
print("  PROBE    $12  =  everything else passing S6ER baseline")
print("="*120)
stats("PREMIUM",  trades_by_tier["PREMIUM"])
stats("STANDARD", trades_by_tier["STANDARD"])
stats("PROBE",    trades_by_tier["PROBE"])
print()
stats("ALL TIERS COMBINED", all_trades)

# What if we used flat $20 for all (current) — recompute for apples-to-apples
print()
print("="*120)
print("APPLES-TO-APPLES vs FLAT $20 (current live)")
print("="*120)

# Recompute everything at flat $20
flat_pnl = 0.0; flat_cost = 0.0; flat_n = 0; flat_w = 0
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
    contracts = max(1, int(20.0 / entry))
    if (direction == "YES" and settle == "yes") or (direction == "NO" and settle == "no"):
        flat_pnl += contracts * (1.0 - entry) - contracts * FEE
        flat_w += 1
    else:
        flat_pnl += -contracts * entry - contracts * FEE
    flat_cost += contracts * entry
    flat_n += 1

tiered_pnl  = sum(t["pnl"]  for t in all_trades)
tiered_cost = sum(t["cost"] for t in all_trades)

print(f"  FLAT $20 (current live):    N={flat_n:>4}  W={flat_w:>3}  WR={100*flat_w/flat_n:5.1f}%  Net=${flat_pnl:+9.2f}  ROI={100*flat_pnl/flat_cost:+6.1f}%")
print(f"  TIERED $35/$20/$12:         N={len(all_trades):>4}  W={sum(1 for t in all_trades if t['win']):>3}  WR={100*sum(1 for t in all_trades if t['win'])/len(all_trades):5.1f}%  Net=${tiered_pnl:+9.2f}  ROI={100*tiered_pnl/tiered_cost:+6.1f}%")
print(f"  Δ Net = ${tiered_pnl - flat_pnl:+.2f}   ({100*(tiered_pnl-flat_pnl)/flat_pnl:+.1f}% vs flat)")

# Probe tier daily breakdown
print()
print("="*120)
print("PROBE TIER ($12) — DAILY BREAKDOWN")
print("="*120)
probe = trades_by_tier["PROBE"]
by_day = defaultdict(list)
for t in probe: by_day[t["date"]].append(t)
for d in sorted(by_day):
    ts=by_day[d]; w=sum(1 for x in ts if x["win"])
    net=sum(x["pnl"] for x in ts); cost=sum(x["cost"] for x in ts)
    roi = 100*net/cost if cost else 0
    print(f"  {d}  N={len(ts):3d}  W={w:3d}  L={len(ts)-w:3d}  WR={100*w/len(ts):5.1f}%  Net=${net:+8.2f}  ROI={roi:+6.1f}%")

# Decompose probe tier
print()
print("="*120)
print("PROBE TIER — DECOMPOSITION (why was this trade demoted to probe?)")
print("="*120)
print()
print("By WHICH baseline rule fails standard:")
stats("  score 70-74 (any entry, any vol)",
      [t for t in probe if t["score"] < 75])
stats("  score>=75 + entry 0.66-0.70",
      [t for t in probe if t["score"] >= 75 and t["entry"] > 0.65])
print()
print("By score band:")
stats("  score 70-74", [t for t in probe if 70 <= t["score"] < 75])
stats("  score 75-79", [t for t in probe if 75 <= t["score"] < 80])
stats("  score 80-84", [t for t in probe if 80 <= t["score"] < 85])
stats("  score 85-89", [t for t in probe if 85 <= t["score"] < 90])
stats("  score 90+",   [t for t in probe if t["score"] >= 90])

print()
print("By entry band (in probe):")
stats("  entry 0.30-0.49", [t for t in probe if 0.30 <= t["entry"] < 0.50])
stats("  entry 0.50-0.65", [t for t in probe if 0.50 <= t["entry"] <= 0.65])
stats("  entry 0.66-0.70", [t for t in probe if 0.65 <  t["entry"] <= 0.70])
