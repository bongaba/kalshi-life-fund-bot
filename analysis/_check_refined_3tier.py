"""Simulate refined 3-tier:

PREMIUM  $35:  (score>=80 AND entry<=0.55 AND vol>=50k) OR vol>=100k
STANDARD $20:  everything else passing S6ER baseline EXCEPT skip zone
SKIP:          score>=75 AND 0.66<=entry<=0.70 AND vol<50k
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
    # Skip dead zone first
    if score >= 75 and 0.66 <= entry <= 0.70 and vol < 50000:
        return "SKIP", 0.0
    # Premium: either tight criteria OR high volume alone
    is_tight = (score >= 80 and entry <= 0.55 and vol >= 50000)
    is_high_vol = (vol >= 100000)
    if is_tight or is_high_vol:
        return "PREMIUM", 35.0
    return "STANDARD", 20.0

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
    if tier == "SKIP":
        trades_by_tier["SKIP"].append({
            "score": score, "entry": entry, "vol": vol,
            "win": (direction == "YES" and settle == "yes") or (direction == "NO" and settle == "no"),
            "date": sig["ts"][:10],
        })
        continue
    contracts = max(1, int(risk / entry))
    if (direction == "YES" and settle == "yes") or (direction == "NO" and settle == "no"):
        win = True; pnl = contracts * (1.0 - entry) - contracts * FEE
    else:
        win = False; pnl = -contracts * entry - contracts * FEE
    cost = contracts * entry
    trades_by_tier[tier].append({
        "score": score, "entry": entry, "vol": vol,
        "win": win, "pnl": pnl, "cost": cost, "date": sig["ts"][:10],
        "contracts": contracts, "risk": risk,
    })

def stats(label, trs, has_pnl=True):
    if not trs:
        print(f"  {label:<55} N=   0"); return
    n=len(trs); w=sum(1 for t in trs if t["win"])
    if has_pnl:
        net=sum(t.get("pnl",0) for t in trs); cost=sum(t.get("cost",0) for t in trs)
        avg_ct = sum(t.get("contracts",0) for t in trs)/n
        print(f"  {label:<55} N={n:>4}  W/L={w:>3}/{n-w:<3}  WR={100*w/n:5.1f}%  Net=${net:+9.2f}  ROI={100*net/cost if cost else 0:+6.1f}%  avg_ct={avg_ct:5.1f}")
    else:
        print(f"  {label:<55} N={n:>4}  W/L={w:>3}/{n-w:<3}  WR={100*w/n:5.1f}%  (skipped — no P&L)")

executed = trades_by_tier["PREMIUM"] + trades_by_tier["STANDARD"]
skipped  = trades_by_tier["SKIP"]
total = executed + skipped

print(f"Total S6ER baseline trades: {len(total)}  (executed: {len(executed)},  skipped: {len(skipped)})")
print()
print("="*120)
print("REFINED 3-TIER (with SKIP zone)")
print("  PREMIUM  $35  =  (score>=80 + entry<=0.55 + vol>=50k)  OR  vol>=100k")
print("  STANDARD $20  =  everything else passing S6ER baseline (except skip)")
print("  SKIP          =  score>=75 + 0.66<=entry<=0.70 + vol<50k")
print("="*120)
stats("PREMIUM",  trades_by_tier["PREMIUM"])
stats("STANDARD", trades_by_tier["STANDARD"])
stats("SKIP (would have been baseline trades)", trades_by_tier["SKIP"], has_pnl=False)
print()
stats("EXECUTED (PREMIUM + STANDARD)", executed)

# What would the skip trades have done?
# Re-evaluate them at flat $20 to see what we're "losing"
skip_pnl_at_20 = 0.0; skip_cost_at_20 = 0.0; skip_w = 0
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
    tier, _ = classify(score, entry, vol)
    if tier != "SKIP":
        continue
    contracts = max(1, int(20.0 / entry))
    if (direction == "YES" and settle == "yes") or (direction == "NO" and settle == "no"):
        skip_pnl_at_20 += contracts * (1.0 - entry) - contracts * FEE
        skip_w += 1
    else:
        skip_pnl_at_20 += -contracts * entry - contracts * FEE
    skip_cost_at_20 += contracts * entry
n_skip = len(skipped)
if n_skip > 0:
    print(f"  WHAT-IF: skipped trades at flat $20:                          "
          f"N={n_skip:>4}  W={skip_w:>3}  WR={100*skip_w/n_skip:5.1f}%  Net=${skip_pnl_at_20:+9.2f}  ROI={100*skip_pnl_at_20/skip_cost_at_20 if skip_cost_at_20 else 0:+6.1f}%")

# Apples-to-apples vs flat $20 (current live)
print()
print("="*120)
print("APPLES-TO-APPLES vs FLAT $20 (current live)")
print("="*120)

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
        flat_pnl += contracts * (1.0 - entry) - contracts * FEE; flat_w += 1
    else:
        flat_pnl += -contracts * entry - contracts * FEE
    flat_cost += contracts * entry
    flat_n += 1

exec_pnl  = sum(t["pnl"]  for t in executed)
exec_cost = sum(t["cost"] for t in executed)
exec_w    = sum(1 for t in executed if t["win"])

print(f"  FLAT $20 (current live):    N={flat_n:>4}  W={flat_w:>3}  WR={100*flat_w/flat_n:5.1f}%  Net=${flat_pnl:+9.2f}  ROI={100*flat_pnl/flat_cost:+6.1f}%")
print(f"  REFINED TIER (skip+sized):  N={len(executed):>4}  W={exec_w:>3}  WR={100*exec_w/len(executed):5.1f}%  Net=${exec_pnl:+9.2f}  ROI={100*exec_pnl/exec_cost:+6.1f}%")
print(f"  Δ Net = ${exec_pnl - flat_pnl:+.2f}   ({100*(exec_pnl-flat_pnl)/flat_pnl:+.1f}% vs flat)")
print(f"  Δ ROI = {100*exec_pnl/exec_cost - 100*flat_pnl/flat_cost:+.1f} pts")

# Daily breakdown
print()
print("="*120)
print("DAILY BREAKDOWN (executed trades only)")
print("="*120)
by_day = defaultdict(list)
for t in executed: by_day[t["date"]].append(t)
for d in sorted(by_day):
    ts=by_day[d]; w=sum(1 for x in ts if x["win"])
    net=sum(x["pnl"] for x in ts); cost=sum(x["cost"] for x in ts)
    roi = 100*net/cost if cost else 0
    n_p = sum(1 for x in ts if x["risk"] == 35.0)
    n_s = sum(1 for x in ts if x["risk"] == 20.0)
    print(f"  {d}  N={len(ts):3d}  (P={n_p:2d}/S={n_s:3d})  W={w:3d}  L={len(ts)-w:3d}  WR={100*w/len(ts):5.1f}%  Net=${net:+8.2f}  ROI={roi:+6.1f}%")

# Bankroll: max concurrent capital required
print()
print("="*120)
print("BANKROLL CHECK")
print("="*120)
print(f"  Total trades executed: {len(executed)} over 16 days = {len(executed)/16:.1f}/day")
print(f"  Premium per day:       {len(trades_by_tier['PREMIUM'])/16:.1f}")
print(f"  Standard per day:      {len(trades_by_tier['STANDARD'])/16:.1f}")
print(f"  Skipped per day:       {len(skipped)/16:.1f}")
