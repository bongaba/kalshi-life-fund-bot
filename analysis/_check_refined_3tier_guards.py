"""Refined 3-tier WITH guards:
  GUARD 1: skip if signal hour (UTC) == 14
  GUARD 2: skip if mins_to_close >= 60

  PREMIUM  $35:  (score>=80 AND entry<=0.55 AND vol>=50k) OR vol>=100k
  STANDARD $20:  everything else passing baseline EXCEPT skip zone
  SKIP zone:     score>=75 AND 0.66<=entry<=0.70 AND vol<50k
"""
import json, os
from datetime import datetime, timezone
from collections import defaultdict

SIGNAL_DIR = "logs/edge_signals"
MARKET_CACHE = os.path.join(SIGNAL_DIR, "_market_cache.json")
FEE = 0.02

def parse_ts(s):
    if s.endswith("Z"): s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)

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
        return "SKIP_DEADZONE", 0.0
    is_tight = (score >= 80 and entry <= 0.55 and vol >= 50000)
    is_high_vol = (vol >= 100000)
    if is_tight or is_high_vol:
        return "PREMIUM", 35.0
    return "STANDARD", 20.0

def simulate(apply_guards):
    by_tier = defaultdict(list)
    skip_hour14 = 0; skip_mins = 0
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
        sig_ts = parse_ts(sig["ts"])
        ct_raw = sig.get("close_time") or mkt.get("close_time")
        mins_to_close = 9999.0
        if ct_raw:
            try: mins_to_close = (parse_ts(ct_raw) - sig_ts).total_seconds() / 60.0
            except Exception: pass

        if apply_guards:
            if sig_ts.hour == 14:
                skip_hour14 += 1; continue
            if mins_to_close >= 60:
                skip_mins += 1; continue

        vol = sig.get("volume") or 0
        tier, risk = classify(score, entry, vol)
        win = (direction == "YES" and settle == "yes") or (direction == "NO" and settle == "no")
        if tier == "SKIP_DEADZONE":
            by_tier["SKIP_DEADZONE"].append({"win": win, "date": sig["ts"][:10]})
            continue
        contracts = max(1, int(risk / entry))
        if win:
            pnl = contracts * (1.0 - entry) - contracts * FEE
        else:
            pnl = -contracts * entry - contracts * FEE
        cost = contracts * entry
        by_tier[tier].append({"win": win, "pnl": pnl, "cost": cost, "risk": risk,
                              "date": sig["ts"][:10], "contracts": contracts})
    return by_tier, skip_hour14, skip_mins

def flat20(apply_guards):
    n=w=0; net=cost=0.0
    for tk, sig in by_ticker.items():
        score = sig.get("score") or 0
        direction = sig.get("direction")
        entry = sig.get("yes_price") if direction == "YES" else sig.get("no_price")
        if entry is None or score < 70 or not (0.30 <= entry <= 0.70): continue
        mkt = cache.get(tk)
        if not mkt or mkt.get("status") not in ("finalized","settled"): continue
        settle = mkt.get("result")
        if settle not in ("yes","no"): continue
        sig_ts = parse_ts(sig["ts"])
        ct_raw = sig.get("close_time") or mkt.get("close_time")
        mtc = 9999.0
        if ct_raw:
            try: mtc = (parse_ts(ct_raw) - sig_ts).total_seconds() / 60.0
            except Exception: pass
        if apply_guards:
            if sig_ts.hour == 14: continue
            if mtc >= 60: continue
        contracts = max(1, int(20.0 / entry))
        win = (direction=="YES" and settle=="yes") or (direction=="NO" and settle=="no")
        if win: net += contracts*(1.0-entry) - contracts*FEE; w += 1
        else:    net += -contracts*entry - contracts*FEE
        cost += contracts*entry; n += 1
    return n, w, net, cost

def stats(label, trs):
    if not trs:
        print(f"  {label:<55} N=   0"); return
    n=len(trs); w=sum(1 for t in trs if t["win"])
    net=sum(t.get("pnl",0) for t in trs); cost=sum(t.get("cost",0) for t in trs)
    avg_ct = sum(t.get("contracts",0) for t in trs)/n if n else 0
    print(f"  {label:<55} N={n:>4}  W/L={w:>3}/{n-w:<3}  WR={100*w/n:5.1f}%  "
          f"Net=${net:+9.2f}  ROI={100*net/cost if cost else 0:+6.1f}%  avg_ct={avg_ct:5.1f}")

print("="*100)
print("REFINED 3-TIER + GUARDS  (skip hour==14, skip mins_to_close>=60)")
print("="*100)
by_tier, sk14, skm = simulate(apply_guards=True)
print(f"  Guard rejections:  hour==14: {sk14}    mins>=60: {skm}")
print()
stats("PREMIUM",  by_tier["PREMIUM"])
stats("STANDARD", by_tier["STANDARD"])
print(f"  SKIP_DEADZONE (would-have-been baseline)              N={len(by_tier['SKIP_DEADZONE']):>4}")
executed = by_tier["PREMIUM"] + by_tier["STANDARD"]
stats("EXECUTED (PREMIUM + STANDARD)", executed)
exec_net = sum(t["pnl"] for t in executed); exec_cost = sum(t["cost"] for t in executed)
exec_w = sum(1 for t in executed if t["win"])

print()
print("="*100)
print("APPLES-TO-APPLES COMPARISONS")
print("="*100)
n,w,net,cost = flat20(apply_guards=False)
print(f"  Flat $20, NO guards (current live):       N={n:>4}  W={w:>3}  WR={100*w/n:5.1f}%  Net=${net:+9.2f}  ROI={100*net/cost:+6.1f}%")
flat_no = net

n,w,net,cost = flat20(apply_guards=True)
print(f"  Flat $20, WITH guards:                    N={n:>4}  W={w:>3}  WR={100*w/n:5.1f}%  Net=${net:+9.2f}  ROI={100*net/cost:+6.1f}%")
flat_yes = net

# Refined tier no guards (from prior sim, recompute here for consistency)
by_tier_ng, _, _ = simulate(apply_guards=False)
exec_ng = by_tier_ng["PREMIUM"] + by_tier_ng["STANDARD"]
ng_net = sum(t["pnl"] for t in exec_ng); ng_cost = sum(t["cost"] for t in exec_ng)
ng_w = sum(1 for t in exec_ng if t["win"])
print(f"  Refined 3-tier, NO guards:                N={len(exec_ng):>4}  W={ng_w:>3}  WR={100*ng_w/len(exec_ng):5.1f}%  Net=${ng_net:+9.2f}  ROI={100*ng_net/ng_cost:+6.1f}%")

print(f"  Refined 3-tier, WITH guards:              N={len(executed):>4}  W={exec_w:>3}  WR={100*exec_w/len(executed):5.1f}%  Net=${exec_net:+9.2f}  ROI={100*exec_net/exec_cost:+6.1f}%")

print()
print(f"  Δ (refined+guards) vs (flat, no guards / live now):  ${exec_net - flat_no:+.2f}  ({100*(exec_net-flat_no)/flat_no:+.1f}%)")
print(f"  Δ (refined+guards) vs (refined, no guards):          ${exec_net - ng_net:+.2f}")
print(f"  Δ (flat+guards) vs (flat, no guards):                ${flat_yes - flat_no:+.2f}")

# Daily breakdown
print()
print("="*100)
print("DAILY (refined + guards)")
print("="*100)
by_day = defaultdict(list)
for t in executed: by_day[t["date"]].append(t)
for d in sorted(by_day):
    ts=by_day[d]; w=sum(1 for x in ts if x["win"])
    net=sum(x["pnl"] for x in ts); cost=sum(x["cost"] for x in ts)
    n_p = sum(1 for x in ts if x["risk"]==35.0); n_s = len(ts)-n_p
    print(f"  {d}  N={len(ts):3d}  (P={n_p:2d}/S={n_s:3d})  W={w:3d}  L={len(ts)-w:3d}  WR={100*w/len(ts):5.1f}%  Net=${net:+8.2f}  ROI={100*net/cost if cost else 0:+6.1f}%")
