"""Refined 3-tier, NO guards, PREMIUM=$40 (= STANDARD*2)"""
import json, os
from collections import defaultdict

SIGNAL_DIR = "logs/edge_signals"
MARKET_CACHE = os.path.join(SIGNAL_DIR, "_market_cache.json")
FEE = 0.02
STANDARD, PREMIUM = 20.0, 40.0

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
    if score >= 75 and 0.66 <= entry <= 0.70 and vol < 50000:
        return "SKIP", 0.0
    if (score >= 80 and entry <= 0.55 and vol >= 50000) or (vol >= 100000):
        return "PREMIUM", PREMIUM
    return "STANDARD", STANDARD

by_tier = defaultdict(list)
for tk, sig in by_ticker.items():
    score = sig.get("score") or 0
    direction = sig.get("direction")
    entry = sig.get("yes_price") if direction == "YES" else sig.get("no_price")
    if entry is None or score < 70 or not (0.30 <= entry <= 0.70): continue
    mkt = cache.get(tk)
    if not mkt or mkt.get("status") not in ("finalized","settled"): continue
    settle = mkt.get("result")
    if settle not in ("yes","no"): continue
    vol = sig.get("volume") or 0
    tier, risk = classify(score, entry, vol)
    if tier == "SKIP":
        by_tier["SKIP"].append({"date": sig["ts"][:10]}); continue
    contracts = max(1, int(risk / entry))
    win = (direction=="YES" and settle=="yes") or (direction=="NO" and settle=="no")
    if win: pnl = contracts*(1.0-entry) - contracts*FEE
    else:    pnl = -contracts*entry - contracts*FEE
    by_tier[tier].append({"win":win,"pnl":pnl,"cost":contracts*entry,"risk":risk,
                          "contracts":contracts,"date":sig["ts"][:10]})

def stats(label,trs):
    if not trs: print(f"  {label:<35} N=   0"); return
    n=len(trs); w=sum(1 for t in trs if t["win"])
    net=sum(t["pnl"] for t in trs); cost=sum(t["cost"] for t in trs)
    avg_ct=sum(t["contracts"] for t in trs)/n
    print(f"  {label:<35} N={n:>4}  W={w:>3}  WR={100*w/n:5.1f}%  "
          f"Net=${net:+9.2f}  ROI={100*net/cost:+6.1f}%  avg_ct={avg_ct:5.1f}")

print(f"PREMIUM=${PREMIUM:.0f}   STANDARD=${STANDARD:.0f}   (no guards, with skip dead zone)")
print("="*100)
stats("PREMIUM",  by_tier["PREMIUM"])
stats("STANDARD", by_tier["STANDARD"])
print(f"  SKIP                                N={len(by_tier['SKIP']):>4}")
exec_=by_tier["PREMIUM"]+by_tier["STANDARD"]
stats("EXECUTED total", exec_)

# Compare to flat $20
flat_n=flat_w=0; flat_net=flat_cost=0.0
for tk, sig in by_ticker.items():
    score = sig.get("score") or 0
    direction = sig.get("direction")
    entry = sig.get("yes_price") if direction == "YES" else sig.get("no_price")
    if entry is None or score < 70 or not (0.30 <= entry <= 0.70): continue
    mkt = cache.get(tk)
    if not mkt or mkt.get("status") not in ("finalized","settled"): continue
    settle = mkt.get("result")
    if settle not in ("yes","no"): continue
    contracts = max(1, int(20.0/entry))
    win = (direction=="YES" and settle=="yes") or (direction=="NO" and settle=="no")
    if win: flat_net += contracts*(1.0-entry) - contracts*FEE; flat_w+=1
    else:    flat_net += -contracts*entry - contracts*FEE
    flat_cost += contracts*entry; flat_n+=1

ex_net=sum(t["pnl"] for t in exec_); ex_cost=sum(t["cost"] for t in exec_)
print()
print("="*100)
print(f"  Flat $20 baseline:  N={flat_n}  Net=${flat_net:+9.2f}  ROI={100*flat_net/flat_cost:+6.1f}%")
print(f"  Refined 3-tier:     N={len(exec_)}  Net=${ex_net:+9.2f}  ROI={100*ex_net/ex_cost:+6.1f}%")
print(f"  Δ Net = ${ex_net-flat_net:+.2f}  ({100*(ex_net-flat_net)/flat_net:+.1f}%)")

# Bankroll check: peak concurrent capital (rough)
print()
print("="*100)
print("BANKROLL FOOTPRINT")
print("="*100)
print(f"  PREMIUM  per day: {len(by_tier['PREMIUM'])/16:.1f}  (~${len(by_tier['PREMIUM'])/16 * PREMIUM:.0f}/day at $40)")
print(f"  STANDARD per day: {len(by_tier['STANDARD'])/16:.1f}  (~${len(by_tier['STANDARD'])/16 * STANDARD:.0f}/day at $20)")
print(f"  Total daily exposure: ~${len(by_tier['PREMIUM'])/16*PREMIUM + len(by_tier['STANDARD'])/16*STANDARD:.0f}/day  spread over 1-hour TTL")
