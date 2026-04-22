"""Quick breakdown of missed wins vs saved losses per stop level."""
import json, os
from collections import defaultdict

signals = []
for fname in sorted(os.listdir("logs/edge_signals")):
    if not fname.endswith(".jsonl"): continue
    with open(os.path.join("logs/edge_signals", fname)) as f:
        for line in f:
            line = line.strip()
            if line:
                try: signals.append(json.loads(line))
                except: pass

by_ticker = defaultdict(list)
for s in signals:
    by_ticker[s["ticker"]].append(s)
for tk in by_ticker:
    by_ticker[tk].sort(key=lambda x: x["ts"])

cache = {}
if os.path.exists("logs/edge_signals/_market_cache.json"):
    with open("logs/edge_signals/_market_cache.json") as f:
        cache = json.load(f)

entries = {}
for s in sorted(signals, key=lambda x: x["ts"]):
    tk = s["ticker"]
    if s.get("filtered"): continue
    d = s.get("direction", "")
    ep = s.get("yes_price", 0) if d == "YES" else s.get("no_price", 0)
    if 0.30 <= ep <= 0.70 and s.get("score", 0) >= 60:
        entries[tk] = s

trades = []
for tk, es in entries.items():
    mkt = cache.get(tk)
    if not mkt: continue
    if mkt.get("status") not in ("finalized", "settled"): continue
    d = es["direction"]
    ep = es.get("yes_price", 0) if d == "YES" else es.get("no_price", 0)
    result = mkt.get("result", "")
    won = (d == "YES" and result == "yes") or (d == "NO" and result == "no")
    subsequent = [s for s in by_ticker[tk] if s["ts"] > es["ts"]]
    checks = []
    for s in subsequent:
        p = s.get("yes_price", 0) if d == "YES" else s.get("no_price", 0)
        checks.append({"price": p})
    trades.append({"ticker": tk, "entry": ep, "won": won, "checks": checks})

header = f"{'Stop %':>7s} | {'Stopped':>7s} | {'Saved':>6s} | {'Missed':>6s} | {'Miss Rate':>9s} | {'Avg Win$ Lost':>13s} | {'Avg Loss$ Saved':>15s}"
print(header)
print("-" * len(header))
for pct in [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.80]:
    stopped = saved = missed = 0
    win_dollars_lost = loss_dollars_saved = 0.0
    for t in trades:
        ep = t["entry"]
        for pc in t["checks"]:
            drop = (ep - pc["price"]) / ep
            if drop >= pct:
                stopped += 1
                if t["won"]:
                    missed += 1
                    would_win = (1.0 - ep) * 100
                    got = (pc["price"] - ep) * 100
                    win_dollars_lost += would_win - got
                else:
                    saved += 1
                    would_lose = ep * 100
                    actual_loss = (ep - pc["price"]) * 100
                    loss_dollars_saved += would_lose - actual_loss
                break
    miss_rate = missed / stopped * 100 if stopped else 0
    avg_wl = win_dollars_lost / missed if missed else 0
    avg_ls = loss_dollars_saved / saved if saved else 0
    print(f"{pct*100:>6.0f}% | {stopped:>7d} | {saved:>6d} | {missed:>6d} | {miss_rate:>8.1f}% | ${avg_wl:>11.2f} | ${avg_ls:>13.2f}")
