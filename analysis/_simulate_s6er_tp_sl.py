"""
S6ER Take-Profit / Stop-Loss simulator.

Strategy S6ER: score >= 70 AND entry_price in [0.30, 0.70].

For each qualifying trade, replay the live position_scores price trajectory:
  - If $PnL reaches >= +$5.00 at any snapshot -> exit as TAKE_PROFIT
  - If $PnL reaches <= -$2.00 at any snapshot -> exit as STOP_LOSS
  - Whichever threshold is hit FIRST wins
  - If neither hit, hold until the last snapshot (proxy for settlement)

Position size: RISK_FLAT = $20.00 per trade.
  contracts = floor($20 / entry_price)
  gross_pnl = contracts * (exit_price - entry_price)
  fees      = 2 * contracts * $0.01  (entry + exit, $0.01/contract typical)
  net_pnl   = gross_pnl - fees

Inputs:
  - logs/edge_signals/edge_signals_*.jsonl   (signal records w/ score, direction, prices)
  - logs/edge_signals/position_scores/pos_scores_*.jsonl  (intra-trade trajectories)

Output: simulate_s6er_tp_sl.txt
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

# ── Config ──
MIN_SCORE = 70.0
ENTRY_MIN = 0.30
ENTRY_MAX = 0.70
TAKE_PROFIT_DOLLARS = 5.00
STOP_LOSS_DOLLARS = -2.00
RISK_FLAT = 20.00
FEE_PER_CONTRACT = 0.01  # per side
BANKROLL_START = 1000.00

SIGNAL_DIR = "logs/edge_signals"
POS_SCORE_DIR = os.path.join(SIGNAL_DIR, "position_scores")
OUT_FILE = "simulate_s6er_tp_sl.txt"


def parse_ts(ts_str: str) -> datetime:
    # Handle both "...Z" and "...+00:00"
    s = ts_str.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


# ── Load signals (S6ER filter applied) ──
print("Loading signals...", flush=True)
signals = []
for fname in sorted(os.listdir(SIGNAL_DIR)):
    if not fname.startswith("edge_signals_") or not fname.endswith(".jsonl"):
        continue
    with open(os.path.join(SIGNAL_DIR, fname), "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                signals.append(json.loads(line))
            except Exception:
                continue

print(f"Total signals loaded: {len(signals)}", flush=True)

# S6ER filter: score >= 70 AND entry in [0.30, 0.70]
# Signals carry yes_price and no_price; entry = yes_price if YES else no_price.
qualifying = []
for s in signals:
    score = s.get("score", 0) or 0
    if score < MIN_SCORE:
        continue
    direction = s.get("direction")
    entry = s.get("yes_price") if direction == "YES" else s.get("no_price")
    if entry is None:
        continue
    if entry < ENTRY_MIN or entry > ENTRY_MAX:
        continue
    qualifying.append({
        "ts": s["ts"],
        "ticker": s["ticker"],
        "direction": direction,
        "entry": entry,
        "score": score,
    })

print(f"S6ER-qualifying signals (score>=70, entry {ENTRY_MIN}-{ENTRY_MAX}): {len(qualifying)}", flush=True)


# ── Load position_scores trajectories ──
print("Loading position_scores trajectories...", flush=True)
# Group by (trade_id, ticker) -> sorted list of snapshots
traj = defaultdict(list)
if os.path.isdir(POS_SCORE_DIR):
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
                if tid is None or tk is None:
                    continue
                traj[(tid, tk)].append(r)

# Sort each trajectory by time
for k in traj:
    traj[k].sort(key=lambda x: x["ts"])

print(f"Distinct live trades with trajectories: {len(traj)}", flush=True)

# Build per-ticker list of trajectories (earliest first) so we can match signals
by_ticker_traj = defaultdict(list)
for (tid, tk), snaps in traj.items():
    if not snaps:
        continue
    first_ts = parse_ts(snaps[0]["ts"])
    last_ts = parse_ts(snaps[-1]["ts"])
    by_ticker_traj[tk].append({
        "trade_id": tid,
        "snaps": snaps,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "direction": snaps[0].get("direction"),
        "entry_price": snaps[0].get("entry_price"),
        "used": False,
    })

# Sort each ticker's trades by first_ts so we can match signals in order
for tk in by_ticker_traj:
    by_ticker_traj[tk].sort(key=lambda x: x["first_ts"])


# ── Match signals to trajectories ──
# A signal matches the earliest unused trajectory for the same ticker whose
# first snapshot is within a reasonable time window (<= 15 min) after the signal
# and whose direction matches.
MATCH_WINDOW_SECONDS = 15 * 60

matched = []
unmatched_count = 0
for sig in qualifying:
    tk = sig["ticker"]
    sig_ts = parse_ts(sig["ts"])
    direction = sig["direction"]
    candidates = by_ticker_traj.get(tk, [])
    chosen = None
    for cand in candidates:
        if cand["used"]:
            continue
        if cand["direction"] != direction:
            continue
        delta = (cand["first_ts"] - sig_ts).total_seconds()
        # allow small negative (signal/pos_score may fire nearly simultaneously)
        if -60 <= delta <= MATCH_WINDOW_SECONDS:
            chosen = cand
            break
    if chosen is None:
        unmatched_count += 1
        continue
    chosen["used"] = True
    matched.append((sig, chosen))

print(f"Matched trades: {len(matched)}", flush=True)
print(f"Unmatched (no live trajectory found): {unmatched_count}", flush=True)


# ── Simulate TP/SL replay ──
def simulate_trade(sig, traj_rec):
    """Walk snapshots, detect first TP or SL hit. Return exit dict."""
    entry = traj_rec.get("entry_price") or sig["entry"]
    if entry <= 0:
        return None
    contracts = int(RISK_FLAT / entry)
    if contracts <= 0:
        return None

    snaps = traj_rec["snaps"]
    direction = sig["direction"]

    # For YES: pnl per contract = current_bid_yes - entry
    # For NO: current_bid in pos_scores is the bid for the NO side (what we could sell at)
    # pos_scores 'current_bid' is the bid on OUR side (sellable price).
    # So pnl_per_contract = current_bid - entry (regardless of YES/NO direction,
    # since entry and bid are already on the same side).

    exit_reason = None
    exit_snap = None
    exit_pnl_dollars = None

    for snap in snaps:
        bid = snap.get("current_bid")
        if bid is None:
            continue
        pnl_per_contract = bid - entry
        pnl_dollars = contracts * pnl_per_contract  # unrealized gross

        if pnl_dollars >= TAKE_PROFIT_DOLLARS:
            exit_reason = "TAKE_PROFIT"
            exit_snap = snap
            exit_pnl_dollars = pnl_dollars
            break
        if pnl_dollars <= STOP_LOSS_DOLLARS:
            exit_reason = "STOP_LOSS"
            exit_snap = snap
            exit_pnl_dollars = pnl_dollars
            break

    if exit_reason is None:
        # Hold to last snapshot (proxy for settlement)
        last = snaps[-1]
        bid = last.get("current_bid", 0)
        pnl_per_contract = bid - entry
        exit_pnl_dollars = contracts * pnl_per_contract
        # If final bid ~ 0.99 and we're long that side, treat as win settle
        # If final bid ~ 0.01 (or 0), treat as loss settle
        if bid >= 0.98:
            exit_reason = "SETTLE_WIN"
        elif bid <= 0.02:
            exit_reason = "SETTLE_LOSS"
        else:
            exit_reason = "SETTLE_OPEN"
        exit_snap = last

    # Subtract fees: entry + exit
    fees = 2 * contracts * FEE_PER_CONTRACT
    net_pnl = exit_pnl_dollars - fees

    return {
        "ticker": sig["ticker"],
        "direction": direction,
        "score": sig["score"],
        "entry": entry,
        "contracts": contracts,
        "exit_reason": exit_reason,
        "exit_bid": exit_snap.get("current_bid"),
        "exit_ts": exit_snap["ts"],
        "gross_pnl": exit_pnl_dollars,
        "fees": fees,
        "net_pnl": net_pnl,
        "duration_s": (parse_ts(exit_snap["ts"]) - parse_ts(snaps[0]["ts"])).total_seconds(),
        "snapshots_used": snaps.index(exit_snap) + 1,
        "total_snapshots": len(snaps),
    }


results = []
for sig, t in matched:
    r = simulate_trade(sig, t)
    if r is not None:
        results.append(r)

print(f"Simulated trades: {len(results)}", flush=True)


# ── Aggregate ──
def pct(n, d):
    return (n / d * 100.0) if d else 0.0


tp_trades = [r for r in results if r["exit_reason"] == "TAKE_PROFIT"]
sl_trades = [r for r in results if r["exit_reason"] == "STOP_LOSS"]
settle_win = [r for r in results if r["exit_reason"] == "SETTLE_WIN"]
settle_loss = [r for r in results if r["exit_reason"] == "SETTLE_LOSS"]
settle_open = [r for r in results if r["exit_reason"] == "SETTLE_OPEN"]

total_net = sum(r["net_pnl"] for r in results)
wins = [r for r in results if r["net_pnl"] > 0]
losses = [r for r in results if r["net_pnl"] <= 0]

# Sequential compounding from $1000 (per-trade $20 risk flat; bankroll just tracks)
bal = BANKROLL_START
peak = BANKROLL_START
max_dd = 0.0
bal_curve = []
# Iterate by entry timestamp
for r in sorted(results, key=lambda x: x["exit_ts"]):
    bal += r["net_pnl"]
    bal_curve.append(bal)
    if bal > peak:
        peak = bal
    dd = (peak - bal) / peak * 100.0 if peak > 0 else 0.0
    if dd > max_dd:
        max_dd = dd


# ── Write report ──
with open(OUT_FILE, "w") as f:
    w = lambda s="": f.write(s + "\n")

    w("=" * 100)
    w("  S6ER TAKE-PROFIT / STOP-LOSS SIMULATION")
    w("=" * 100)
    w(f"  Generated: {datetime.now().isoformat(timespec='seconds')}")
    w("")
    w("  Strategy filter: score >= 70 AND entry_price in [$0.30, $0.70]")
    w(f"  Take-profit:     exit when unrealized $PnL >= +${TAKE_PROFIT_DOLLARS:.2f}")
    w(f"  Stop-loss:       exit when unrealized $PnL <= ${STOP_LOSS_DOLLARS:+.2f}")
    w(f"  Position size:   ${RISK_FLAT:.2f} per trade (flat)")
    w(f"  Fees:            ${FEE_PER_CONTRACT:.2f}/contract per side (entry + exit)")
    w(f"  Bankroll start:  ${BANKROLL_START:.2f}")
    w("")
    w("-" * 100)
    w("  DATA AVAILABILITY")
    w("-" * 100)
    w(f"  Total signals loaded:               {len(signals)}")
    w(f"  S6ER-qualifying signals:            {len(qualifying)}")
    w(f"  Live trade trajectories available:  {len(traj)}")
    w(f"  Matched signal -> trajectory:       {len(matched)}")
    w(f"  Unmatched (no live trajectory):     {unmatched_count}")
    w(f"  Simulated trades:                   {len(results)}")
    w("")
    w("  NOTE: Only trades with a live position_scores trajectory can be replayed.")
    w("        Earliest pos_scores file is 2026-04-18, so simulation covers trades")
    w("        from that date onward.")
    w("")
    w("-" * 100)
    w("  EXIT-REASON BREAKDOWN")
    w("-" * 100)
    n = len(results) or 1
    w(f"  TAKE_PROFIT (+${TAKE_PROFIT_DOLLARS:.2f}):    {len(tp_trades):4d}  ({pct(len(tp_trades), n):5.1f}%)")
    w(f"  STOP_LOSS   (${STOP_LOSS_DOLLARS:+.2f}):     {len(sl_trades):4d}  ({pct(len(sl_trades), n):5.1f}%)")
    w(f"  SETTLE_WIN  (held to 0.99):    {len(settle_win):4d}  ({pct(len(settle_win), n):5.1f}%)")
    w(f"  SETTLE_LOSS (held to 0.01):    {len(settle_loss):4d}  ({pct(len(settle_loss), n):5.1f}%)")
    w(f"  SETTLE_OPEN (still mid-range): {len(settle_open):4d}  ({pct(len(settle_open), n):5.1f}%)")
    w("")
    w("-" * 100)
    w("  P&L SUMMARY")
    w("-" * 100)
    w(f"  Trades:                  {len(results)}")
    w(f"  Wins  (net > 0):         {len(wins)}  ({pct(len(wins), n):.1f}%)")
    w(f"  Losses (net <= 0):       {len(losses)}  ({pct(len(losses), n):.1f}%)")
    w(f"  Total net P&L:           ${total_net:+,.2f}")
    if results:
        w(f"  Avg net P&L / trade:     ${total_net / len(results):+,.2f}")
        w(f"  Best trade:              ${max(r['net_pnl'] for r in results):+,.2f}")
        w(f"  Worst trade:             ${min(r['net_pnl'] for r in results):+,.2f}")
    w("")
    w(f"  Sequential end balance:  ${bal:,.2f}")
    w(f"  Sequential ROI:          {((bal - BANKROLL_START) / BANKROLL_START * 100):+.1f}%")
    w(f"  Max drawdown:            {max_dd:.1f}%")
    w("")
    w("-" * 100)
    w("  P&L BY EXIT REASON")
    w("-" * 100)
    for label, bucket in [
        ("TAKE_PROFIT", tp_trades),
        ("STOP_LOSS", sl_trades),
        ("SETTLE_WIN", settle_win),
        ("SETTLE_LOSS", settle_loss),
        ("SETTLE_OPEN", settle_open),
    ]:
        if not bucket:
            w(f"  {label:12s}  (none)")
            continue
        s = sum(r["net_pnl"] for r in bucket)
        avg = s / len(bucket)
        w(f"  {label:12s}  count={len(bucket):4d}  total=${s:+,.2f}  avg=${avg:+,.2f}")
    w("")
    w("-" * 100)
    w("  TRADE-BY-TRADE DETAIL (first 200)")
    w("-" * 100)
    w(f"  {'#':>4} {'ts':<19} {'ticker':<36} {'dir':<4} {'score':>5} "
      f"{'entry':>6} {'ctr':>4} {'exit_bid':>8} {'reason':<12} "
      f"{'gross':>9} {'fees':>6} {'net':>9} {'dur_s':>7}")
    for i, r in enumerate(sorted(results, key=lambda x: x["exit_ts"])[:200], start=1):
        w(f"  {i:>4} {r['exit_ts'][:19]:<19} {r['ticker']:<36} {r['direction']:<4} "
          f"{r['score']:>5.0f} {r['entry']:>6.3f} {r['contracts']:>4d} "
          f"{(r['exit_bid'] or 0):>8.3f} {r['exit_reason']:<12} "
          f"${r['gross_pnl']:>+7.2f} ${r['fees']:>4.2f} ${r['net_pnl']:>+7.2f} "
          f"{r['duration_s']:>7.0f}")
    if len(results) > 200:
        w(f"  ... and {len(results) - 200} more")
    w("")
    w("=" * 100)
    w("  END OF REPORT")
    w("=" * 100)

print(f"\nResults written to {OUT_FILE}")
print(f"Net P&L: ${total_net:+,.2f} | End balance: ${bal:,.2f} "
      f"| TP={len(tp_trades)} SL={len(sl_trades)} "
      f"W={len(wins)} L={len(losses)}")
