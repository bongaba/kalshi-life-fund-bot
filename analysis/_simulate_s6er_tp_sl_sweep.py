"""
S6ER TP/SL parameter sweep.

Reuses the matched (signal -> trajectory) dataset built in _simulate_s6er_tp_sl.py,
but sweeps a grid of (take_profit, stop_loss) thresholds.

Output: simulate_s6er_tp_sl_sweep.txt
"""
import json
import os
from collections import defaultdict
from datetime import datetime

MIN_SCORE = 70.0
ENTRY_MIN = 0.30
ENTRY_MAX = 0.70
RISK_FLAT = 20.00
FEE_PER_CONTRACT = 0.01
BANKROLL_START = 1000.00

TP_GRID = [3.00, 4.00, 5.00, 6.00, 8.00, 10.00, 12.00, 15.00]
SL_GRID = [-1.00, -2.00, -3.00, -4.00, -5.00, -6.00, -8.00, -10.00]

SIGNAL_DIR = "logs/edge_signals"
POS_SCORE_DIR = os.path.join(SIGNAL_DIR, "position_scores")
OUT_FILE = "simulate_s6er_tp_sl_sweep.txt"


def parse_ts(ts_str):
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


# ── Load signals + trajectories (same as main sim) ──
print("Loading signals...", flush=True)
signals = []
for fname in sorted(os.listdir(SIGNAL_DIR)):
    if not fname.startswith("edge_signals_") or not fname.endswith(".jsonl"):
        continue
    with open(os.path.join(SIGNAL_DIR, fname), "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    signals.append(json.loads(line))
                except Exception:
                    pass

qualifying = []
for s in signals:
    if (s.get("score") or 0) < MIN_SCORE:
        continue
    d = s.get("direction")
    entry = s.get("yes_price") if d == "YES" else s.get("no_price")
    if entry is None or entry < ENTRY_MIN or entry > ENTRY_MAX:
        continue
    qualifying.append({"ts": s["ts"], "ticker": s["ticker"], "direction": d,
                       "entry": entry, "score": s.get("score", 0)})

print(f"S6ER-qualifying signals: {len(qualifying)}", flush=True)

print("Loading position_scores...", flush=True)
traj = defaultdict(list)
for fname in sorted(os.listdir(POS_SCORE_DIR)):
    if not fname.startswith("pos_scores_") or not fname.endswith(".jsonl"):
        continue
    with open(os.path.join(POS_SCORE_DIR, fname), "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            tid = r.get("trade_id")
            tk = r.get("ticker")
            if tid is not None and tk is not None:
                traj[(tid, tk)].append(r)

for k in traj:
    traj[k].sort(key=lambda x: x["ts"])

by_ticker_traj = defaultdict(list)
for (tid, tk), snaps in traj.items():
    if not snaps:
        continue
    by_ticker_traj[tk].append({
        "trade_id": tid,
        "snaps": snaps,
        "first_ts": parse_ts(snaps[0]["ts"]),
        "direction": snaps[0].get("direction"),
        "entry_price": snaps[0].get("entry_price"),
        "used": False,
    })
for tk in by_ticker_traj:
    by_ticker_traj[tk].sort(key=lambda x: x["first_ts"])

MATCH_WINDOW_SECONDS = 15 * 60
matched = []
for sig in qualifying:
    tk = sig["ticker"]
    sig_ts = parse_ts(sig["ts"])
    d = sig["direction"]
    for cand in by_ticker_traj.get(tk, []):
        if cand["used"] or cand["direction"] != d:
            continue
        delta = (cand["first_ts"] - sig_ts).total_seconds()
        if -60 <= delta <= MATCH_WINDOW_SECONDS:
            cand["used"] = True
            matched.append((sig, cand))
            break

print(f"Matched trades: {len(matched)}", flush=True)


# ── Sweep ──
def simulate(tp_dollars, sl_dollars):
    results = []
    for sig, t in matched:
        entry = t.get("entry_price") or sig["entry"]
        if entry <= 0:
            continue
        contracts = int(RISK_FLAT / entry)
        if contracts <= 0:
            continue
        snaps = t["snaps"]
        exit_reason = None
        exit_pnl = None
        for snap in snaps:
            bid = snap.get("current_bid")
            if bid is None:
                continue
            pnl = contracts * (bid - entry)
            if pnl >= tp_dollars:
                exit_reason = "TP"
                exit_pnl = pnl
                break
            if pnl <= sl_dollars:
                exit_reason = "SL"
                exit_pnl = pnl
                break
        if exit_reason is None:
            last = snaps[-1]
            bid = last.get("current_bid", 0) or 0
            exit_pnl = contracts * (bid - entry)
            if bid >= 0.98:
                exit_reason = "SETTLE_WIN"
            elif bid <= 0.02:
                exit_reason = "SETTLE_LOSS"
            else:
                exit_reason = "SETTLE_OPEN"
        fees = 2 * contracts * FEE_PER_CONTRACT
        net = exit_pnl - fees
        results.append({"net": net, "reason": exit_reason, "exit_ts": snaps[-1]["ts"]
                        if exit_reason.startswith("SETTLE") else snap["ts"]})
    return results


def summarize(results):
    n = len(results)
    if n == 0:
        return None
    total_net = sum(r["net"] for r in results)
    wins = sum(1 for r in results if r["net"] > 0)
    tp = sum(1 for r in results if r["reason"] == "TP")
    sl = sum(1 for r in results if r["reason"] == "SL")
    sw = sum(1 for r in results if r["reason"] == "SETTLE_WIN")
    sloss = sum(1 for r in results if r["reason"] == "SETTLE_LOSS")
    so = sum(1 for r in results if r["reason"] == "SETTLE_OPEN")

    bal = BANKROLL_START
    peak = BANKROLL_START
    max_dd = 0.0
    for r in sorted(results, key=lambda x: x["exit_ts"]):
        bal += r["net"]
        if bal > peak:
            peak = bal
        dd = (peak - bal) / peak * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    roi = (bal - BANKROLL_START) / BANKROLL_START * 100.0
    return {
        "n": n, "total_net": total_net, "wins": wins, "losses": n - wins,
        "win_rate": wins / n * 100.0, "tp": tp, "sl": sl, "sw": sw, "sloss": sloss, "so": so,
        "end_bal": bal, "roi": roi, "max_dd": max_dd,
        "avg": total_net / n,
    }


grid_results = []
for tp in TP_GRID:
    for sl in SL_GRID:
        r = simulate(tp, sl)
        s = summarize(r)
        grid_results.append((tp, sl, s))

# Best by ROI
grid_results.sort(key=lambda x: (x[2]["roi"] if x[2] else -9e9), reverse=True)


with open(OUT_FILE, "w") as f:
    w = lambda s="": f.write(s + "\n")
    w("=" * 110)
    w("  S6ER TAKE-PROFIT / STOP-LOSS PARAMETER SWEEP")
    w("=" * 110)
    w(f"  Generated: {datetime.now().isoformat(timespec='seconds')}")
    w(f"  Strategy: score>=70, entry in [$0.30, $0.70]")
    w(f"  Position size: ${RISK_FLAT:.2f} flat, fees ${FEE_PER_CONTRACT:.2f}/ctr/side")
    w(f"  Trades in dataset: {len(matched)}")
    w("")
    w("  Grid:")
    w(f"    TP candidates: {TP_GRID}")
    w(f"    SL candidates: {SL_GRID}")
    w("")
    w("-" * 110)
    w("  RESULTS SORTED BY ROI (best first)")
    w("-" * 110)
    w(f"  {'TP':>6} {'SL':>6} | {'Trades':>6} {'WR%':>5} "
      f"{'TP':>4} {'SL':>4} {'SW':>4} {'SL*':>4} {'SO':>4} | "
      f"{'Net':>10} {'Avg':>7} {'EndBal':>10} {'ROI%':>7} {'MaxDD%':>7}")
    w("  " + "-" * 106)
    for tp, sl, s in grid_results:
        if s is None:
            continue
        w(f"  ${tp:>4.2f} ${sl:>4.2f} | {s['n']:>6d} {s['win_rate']:>5.1f} "
          f"{s['tp']:>4d} {s['sl']:>4d} {s['sw']:>4d} {s['sloss']:>4d} {s['so']:>4d} | "
          f"${s['total_net']:>+8.2f} ${s['avg']:>+5.2f} ${s['end_bal']:>8.2f} "
          f"{s['roi']:>+6.1f}% {s['max_dd']:>6.1f}%")
    w("")
    w("  SL* = SETTLE_LOSS (held to 0.01).  SO = SETTLE_OPEN (neither threshold nor decisive settle).")
    w("")
    w("=" * 110)
    w("  TOP-5 HEATMAP VIEW — ROI by (TP rows, SL cols)")
    w("=" * 110)
    label = "TP\\SL"
    hdr = f"  {label:>7} " + " ".join(f"{sl:>+7.2f}" for sl in SL_GRID)
    w(hdr)
    for tp in TP_GRID:
        row = [f"  ${tp:>5.2f}  "]
        for sl in SL_GRID:
            s = next((x[2] for x in grid_results if x[0] == tp and x[1] == sl), None)
            if s is None:
                row.append(f"{'--':>7}")
            else:
                row.append(f"{s['roi']:>+6.1f}%")
        w(" ".join(row))
    w("")
    w("=" * 110)

print(f"Wrote {OUT_FILE}")
print("Top 5:")
for tp, sl, s in grid_results[:5]:
    print(f"  TP=${tp:.2f} SL=${sl:.2f} -> ROI={s['roi']:+.1f}% WR={s['win_rate']:.1f}% "
          f"Net=${s['total_net']:+.2f} DD={s['max_dd']:.1f}%")
