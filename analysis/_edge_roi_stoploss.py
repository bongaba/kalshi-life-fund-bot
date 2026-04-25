"""
Edge ROI analysis WITH 70% stop loss factored in.
Re-uses cached market data from the previous analysis.
Compares key strategies with and without stop loss.
"""
import json, os, sys, time, base64, math
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import MODE, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

FEE_PER_CONTRACT = 0.02
BANKROLL = 1000.00
STOP_LOSS_PCT = 0.70  # exit when position is down 70%

# ── Load signals ──
signals = []
for fname in sorted(os.listdir("logs/edge_signals")):
    if not fname.endswith(".jsonl"):
        continue
    with open(os.path.join("logs/edge_signals", fname), "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                signals.append(json.loads(line))
            except:
                continue

print(f"Total signals loaded: {len(signals)}", flush=True)

# Deduplicate: keep last signal per ticker
by_ticker = {}
for s in signals:
    tk = s["ticker"]
    if tk not in by_ticker or s["ts"] > by_ticker[tk]["ts"]:
        by_ticker[tk] = s

filtered_tickers = {tk: s for tk, s in by_ticker.items() if s.get("filtered")}
unfiltered_tickers = {tk: s for tk, s in by_ticker.items() if not s.get("filtered")}
print(f"Unique tickers: {len(by_ticker)}", flush=True)

# ── Load cached market data ──
MARKET_CACHE_FILE = "logs/edge_signals/_market_cache.json"
cache = {}
if os.path.exists(MARKET_CACHE_FILE):
    with open(MARKET_CACHE_FILE, "r") as f:
        cache = json.load(f)
print(f"Loaded {len(cache)} cached markets", flush=True)

# Build trade records
all_trades = []
for tk, sig in sorted(by_ticker.items(), key=lambda x: x[1]["ts"]):
    mkt = cache.get(tk)
    if not mkt:
        continue
    status = mkt.get("status", "")
    result = mkt.get("result", "")
    if status not in ("finalized", "settled"):
        continue

    d = sig["direction"]
    entry = sig.get("yes_price", 0) if d == "YES" else sig.get("no_price", 0)
    won = (d == "YES" and result == "yes") or (d == "NO" and result == "no")
    is_filtered = sig.get("filtered", False)
    score = sig.get("score", 0)
    settle_time = mkt.get("settlement_ts") or mkt.get("close_time") or ""

    all_trades.append({
        "ticker": tk, "direction": d, "score": score, "entry": entry,
        "won": won, "filtered": is_filtered,
        "filter_reasons": sig.get("filter_reasons", []),
        "ts": sig["ts"], "settle_time": settle_time,
    })

unfiltered_settled = [t for t in all_trades if not t["filtered"]]
filtered_settled = [t for t in all_trades if t["filtered"]]
print(f"Settled trades: {len(all_trades)} ({len(unfiltered_settled)} unfiltered, {len(filtered_settled)} filtered)", flush=True)


# ── ROI calc with optional stop loss ──
def calc_strategy(trades, bankroll, stop_loss_pct=None):
    n = len(trades)
    if n == 0:
        return None
    per_trade = bankroll / n
    total_gross = 0.0
    total_fees = 0.0
    wins = 0
    losses = 0
    rows = []
    for t in sorted(trades, key=lambda x: x["ts"]):
        entry = t["entry"]
        if entry <= 0:
            continue
        contracts = math.floor(per_trade / entry)
        if contracts <= 0:
            continue
        fees = contracts * FEE_PER_CONTRACT
        if t["won"]:
            gross = contracts * (1.0 - entry)
            wl = "WIN"
            wins += 1
        else:
            if stop_loss_pct is not None:
                # Stop loss caps loss at stop_loss_pct of entry
                gross = -(contracts * entry * stop_loss_pct)
            else:
                gross = -(contracts * entry)
            wl = "LOSS"
            losses += 1
        net = gross - fees
        total_gross += gross
        total_fees += fees
        rows.append({
            "ticker": t["ticker"], "direction": t["direction"],
            "score": t["score"], "entry": entry, "contracts": contracts,
            "wl": wl, "gross": gross, "fees": fees, "net": net,
        })
    total_net = total_gross - total_fees
    roi = (total_net / bankroll) * 100
    return {
        "rows": rows, "total_gross": total_gross, "total_fees": total_fees,
        "total_net": total_net, "roi": roi, "wins": wins, "losses": losses,
        "per_trade": per_trade, "n": n,
    }


def sequential_sim(trades, bankroll, risk_per_trade=100.0, max_trades_per_day=None,
                    slippage_cents=1, fill_rate=1.0, stop_loss_pct=None):
    import random

    def parse_ts(s):
        s = s.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except:
            return datetime.min.replace(tzinfo=timezone.utc)

    sorted_trades = sorted(trades, key=lambda x: x["ts"])
    if not sorted_trades:
        return None

    available = bankroll
    locked = 0.0
    peak_total = bankroll
    max_dd = 0.0
    wins = 0
    losses = 0
    total_fees = 0.0
    open_positions = []
    trades_today = 0
    current_day = None
    random.seed(42)
    rows = []

    for t in sorted_trades:
        entry_time = parse_ts(t["ts"])
        settle_time_str = t.get("settle_time", "")
        settle_dt = parse_ts(settle_time_str) if settle_time_str else entry_time

        trade_day = entry_time.date()
        if trade_day != current_day:
            current_day = trade_day
            trades_today = 0

        # Free settled positions
        still_open = []
        for pos in open_positions:
            if pos["settle_dt"] <= entry_time:
                available += pos["cost"] + pos["pnl"]
                locked -= pos["cost"]
            else:
                still_open.append(pos)
        open_positions = still_open

        entry_price = t["entry"]
        if entry_price <= 0:
            continue

        if max_trades_per_day is not None and trades_today >= max_trades_per_day:
            total_balance = available + locked
            rows.append({"ticker": t["ticker"], "direction": t["direction"],
                "score": t["score"], "entry": entry_price, "contracts": 0,
                "wl": "SKIP", "gross": 0, "fees": 0, "net": 0,
                "available": available, "locked": locked, "balance": total_balance, "ts": t["ts"]})
            continue

        if fill_rate < 1.0 and random.random() > fill_rate:
            total_balance = available + locked
            rows.append({"ticker": t["ticker"], "direction": t["direction"],
                "score": t["score"], "entry": entry_price, "contracts": 0,
                "wl": "SKIP", "gross": 0, "fees": 0, "net": 0,
                "available": available, "locked": locked, "balance": total_balance, "ts": t["ts"]})
            continue

        entry_price = min(entry_price + slippage_cents / 100.0, 0.99)
        alloc = min(risk_per_trade, available)
        contracts = math.floor(alloc / entry_price)
        if contracts <= 0:
            total_balance = available + locked
            rows.append({"ticker": t["ticker"], "direction": t["direction"],
                "score": t["score"], "entry": entry_price, "contracts": 0,
                "wl": "SKIP", "gross": 0, "fees": 0, "net": 0,
                "available": available, "locked": locked, "balance": total_balance, "ts": t["ts"]})
            continue

        cost = contracts * entry_price
        fees = contracts * FEE_PER_CONTRACT
        trades_today += 1

        if t["won"]:
            gross = contracts * (1.0 - entry_price)
            wl = "WIN"
            wins += 1
        else:
            if stop_loss_pct is not None:
                # With stop loss: lose only stop_loss_pct of entry cost
                gross = -(contracts * entry_price * stop_loss_pct)
                # Capital returned early = cost - |gross| (partial recovery)
            else:
                gross = -(contracts * entry_price)
            wl = "LOSS"
            losses += 1

        net = gross - fees
        total_fees += fees
        available -= cost
        locked += cost
        pnl = net
        open_positions.append({"settle_dt": settle_dt, "cost": cost, "pnl": pnl})

        pending_pnl = sum(p["pnl"] for p in open_positions)
        true_balance = available + locked + pending_pnl

        if true_balance > peak_total:
            peak_total = true_balance
        dd = (peak_total - true_balance) / peak_total * 100 if peak_total > 0 else 0
        if dd > max_dd:
            max_dd = dd

        rows.append({"ticker": t["ticker"], "direction": t["direction"],
            "score": t["score"], "entry": entry_price, "contracts": contracts,
            "wl": wl, "gross": gross, "fees": fees, "net": net,
            "available": available, "locked": locked, "balance": true_balance, "ts": t["ts"]})

    # Settle remaining
    for pos in open_positions:
        available += pos["cost"] + pos["pnl"]
        locked -= pos["cost"]

    final_balance = available
    total_net = final_balance - bankroll
    roi = (total_net / bankroll) * 100

    return {
        "rows": rows, "balance": final_balance, "total_net": total_net,
        "total_fees": total_fees, "roi": roi, "wins": wins, "losses": losses,
        "max_dd": max_dd, "peak": peak_total,
        "n": len([r for r in rows if r["wl"] != "SKIP"]),
        "skipped": len([r for r in rows if r["wl"] == "SKIP"]),
    }


# ── Strategy definitions ──
strategies = {
    "S1: All Unfiltered": unfiltered_settled,
    "S2: Unfiltered, Score>=70": [t for t in unfiltered_settled if t["score"] >= 70],
    "S3: Unfiltered, Score>=80": [t for t in unfiltered_settled if t["score"] >= 80],
    "S4: Unfiltered, Entry $0.30-$0.70": [t for t in unfiltered_settled if 0.30 <= t["entry"] <= 0.70],
    "S5: All Signals (no filters)": all_trades,
    "S6: All Signals, Score>=70": [t for t in all_trades if t["score"] >= 70],
    "S7: Filters Re-Applied": [t for t in all_trades
        if 0.30 <= t["entry"] <= 0.85
        and not any("imbal_low" in r for r in t.get("filter_reasons", []))
        and not t["filtered"]],
    "S8: Filtered-Only (rejected)": filtered_settled,
}

# ── Output ──
output = []
output.append("=" * 110)
output.append("  EDGE SCANNER ROI ANALYSIS — WITH vs WITHOUT 70% STOP LOSS")
output.append(f"  Stop loss: exit when position is down {STOP_LOSS_PCT*100:.0f}% (sell at {(1-STOP_LOSS_PCT)*100:.0f}% of entry)")
output.append(f"  Data: {len(signals)} signals, {len(by_ticker)} tickers, {len(all_trades)} settled trades")
output.append(f"  Date range: {min(s['ts'] for s in signals)[:10]} to {max(s['ts'] for s in signals)[:10]}")
output.append(f"  Bankroll: $1,000")
output.append(f"  NOTE: Stop loss assumes all losing trades are exited at exactly 70% loss.")
output.append(f"        In reality some fast-settling markets may gap past the stop loss.")
output.append("=" * 110)


# ── SECTION 1: Even-Split comparison ──
output.append(f"\n\n{'='*110}")
output.append(f"  EVEN-SPLIT COMPARISON — $1,000 bankroll divided evenly across all trades")
output.append(f"{'='*110}")
output.append(f"  {'Strategy':<40s} | {'No SL ROI':>10s} | {'70% SL ROI':>11s} | {'Improvement':>12s} | {'Trades':>7s} | {'W/L':>18s} | {'No SL Loss $':>12s} | {'SL Loss $':>10s}")
output.append(f"  {'-'*135}")

for label, strades in strategies.items():
    no_sl = calc_strategy(strades, BANKROLL, stop_loss_pct=None)
    with_sl = calc_strategy(strades, BANKROLL, stop_loss_pct=STOP_LOSS_PCT)
    if no_sl and with_sl:
        total = no_sl['wins'] + no_sl['losses']
        wr = no_sl['wins'] / total * 100 if total else 0
        improvement = with_sl['roi'] - no_sl['roi']
        # Calculate total loss $ for comparison
        no_sl_loss = sum(r['gross'] for r in no_sl['rows'] if r['wl'] == 'LOSS')
        sl_loss = sum(r['gross'] for r in with_sl['rows'] if r['wl'] == 'LOSS')
        output.append(
            f"  {label:<40s} | {no_sl['roi']:>+9.1f}% | {with_sl['roi']:>+10.1f}% | {improvement:>+11.1f}% | {total:>7d} | {no_sl['wins']}W/{no_sl['losses']}L ({wr:.0f}%) | ${no_sl_loss:>+10.2f} | ${sl_loss:>+9.2f}"
        )

# ── SECTION 2: Sequential (compounding) comparison ──
output.append(f"\n\n{'='*110}")
output.append(f"  SEQUENTIAL COMPARISON — $100/trade, capital locked until settlement")
output.append(f"{'='*110}")
output.append(f"  {'Strategy':<40s} | {'No SL ROI':>10s} | {'SL ROI':>10s} | {'No SL DD':>9s} | {'SL DD':>8s} | {'No SL End':>12s} | {'SL End':>12s} | {'W/L':>18s}")
output.append(f"  {'-'*135}")

for label, strades in strategies.items():
    no_sl = sequential_sim(strades, BANKROLL, 100.0, stop_loss_pct=None)
    with_sl = sequential_sim(strades, BANKROLL, 100.0, stop_loss_pct=STOP_LOSS_PCT)
    if no_sl and with_sl:
        total = no_sl['wins'] + no_sl['losses']
        wr = no_sl['wins'] / total * 100 if total else 0
        output.append(
            f"  {label:<40s} | {no_sl['roi']:>+9.1f}% | {with_sl['roi']:>+9.1f}% | {no_sl['max_dd']:>8.1f}% | {with_sl['max_dd']:>7.1f}% | ${no_sl['balance']:>10,.2f} | ${with_sl['balance']:>10,.2f} | {no_sl['wins']}W/{no_sl['losses']}L ({wr:.0f}%)"
        )


# ── SECTION 3: Realistic comparison (capped trades, slippage, fill rate) ──
output.append(f"\n\n{'='*110}")
output.append(f"  REALISTIC COMPARISON — Max 20 trades/day, 2¢ slippage, 80% fill, $100/trade")
output.append(f"{'='*110}")
output.append(f"  {'Strategy':<40s} | {'No SL ROI':>10s} | {'SL ROI':>10s} | {'No SL DD':>9s} | {'SL DD':>8s} | {'No SL End':>12s} | {'SL End':>12s} | {'W/L':>18s}")
output.append(f"  {'-'*135}")

realistic_keys = [
    "S4: Unfiltered, Entry $0.30-$0.70",
    "S5: All Signals (no filters)",
    "S6: All Signals, Score>=70",
    "S7: Filters Re-Applied",
    "S8: Filtered-Only (rejected)",
]
for label in realistic_keys:
    strades = strategies[label]
    no_sl = sequential_sim(strades, BANKROLL, 100.0, max_trades_per_day=20, slippage_cents=2, fill_rate=0.80, stop_loss_pct=None)
    with_sl = sequential_sim(strades, BANKROLL, 100.0, max_trades_per_day=20, slippage_cents=2, fill_rate=0.80, stop_loss_pct=STOP_LOSS_PCT)
    if no_sl and with_sl:
        total = no_sl['wins'] + no_sl['losses']
        wr = no_sl['wins'] / total * 100 if total else 0
        output.append(
            f"  {label:<40s} | {no_sl['roi']:>+9.1f}% | {with_sl['roi']:>+9.1f}% | {no_sl['max_dd']:>8.1f}% | {with_sl['max_dd']:>7.1f}% | ${no_sl['balance']:>10,.2f} | ${with_sl['balance']:>10,.2f} | {no_sl['wins']}W/{no_sl['losses']}L ({wr:.0f}%)"
        )


# ── SECTION 4: Impact breakdown — how much does SL save per loss? ──
output.append(f"\n\n{'='*110}")
output.append(f"  STOP LOSS IMPACT BREAKDOWN — How much does the 70% SL save per strategy?")
output.append(f"{'='*110}")

for label in ["S4: Unfiltered, Entry $0.30-$0.70", "S6: All Signals, Score>=70"]:
    strades = strategies[label]
    output.append(f"\n  ── {label} ──")
    output.append(f"  {'Entry Range':<20s} | {'Losses':>7s} | {'Full Loss $':>12s} | {'SL Loss $':>12s} | {'Saved $':>10s} | {'Avg Entry':>10s} | {'Avg Saved/Trade':>15s}")
    output.append(f"  {'-'*100}")

    # Group losing trades by entry price range
    losing = [t for t in strades if not t["won"] and t["entry"] > 0]
    ranges = [
        ("$0.01-$0.30", 0.01, 0.30),
        ("$0.31-$0.50", 0.31, 0.50),
        ("$0.51-$0.70", 0.51, 0.70),
        ("$0.71-$0.85", 0.71, 0.85),
        ("$0.86-$0.99", 0.86, 0.99),
    ]
    total_full = 0
    total_sl = 0
    total_count = 0
    for range_label, lo, hi in ranges:
        bucket = [t for t in losing if lo <= t["entry"] <= hi]
        if not bucket:
            output.append(f"  {range_label:<20s} | {0:>7d} | {'$0.00':>12s} | {'$0.00':>12s} | {'$0.00':>10s} | {'-':>10s} | {'-':>15s}")
            continue
        # Compute with 100 per trade allocation
        full_loss = 0
        sl_loss = 0
        for t in bucket:
            contracts = math.floor(100.0 / t["entry"])
            if contracts <= 0:
                continue
            full_loss += contracts * t["entry"]
            sl_loss += contracts * t["entry"] * STOP_LOSS_PCT
        saved = full_loss - sl_loss
        avg_entry = sum(t["entry"] for t in bucket) / len(bucket)
        avg_saved = saved / len(bucket) if bucket else 0
        total_full += full_loss
        total_sl += sl_loss
        total_count += len(bucket)
        output.append(
            f"  {range_label:<20s} | {len(bucket):>7d} | ${full_loss:>10.2f} | ${sl_loss:>10.2f} | ${saved:>8.2f} | ${avg_entry:>9.3f} | ${avg_saved:>13.2f}"
        )
    total_saved = total_full - total_sl
    output.append(f"  {'-'*100}")
    output.append(f"  {'TOTAL':<20s} | {total_count:>7d} | ${total_full:>10.2f} | ${total_sl:>10.2f} | ${total_saved:>8.2f} | {'':>10s} | ${total_saved/total_count if total_count else 0:>13.2f}")


# ── SECTION 5: Best strategy with stop loss — the final answer ──
output.append(f"\n\n{'='*110}")
output.append(f"  FINAL VERDICT — Which strategy + stop loss combo is best?")
output.append(f"{'='*110}")

# Compute all realistic + SL combos
verdicts = []
for label in realistic_keys:
    strades = strategies[label]
    r = sequential_sim(strades, BANKROLL, 100.0, max_trades_per_day=20, slippage_cents=2, fill_rate=0.80, stop_loss_pct=STOP_LOSS_PCT)
    if r:
        total = r['wins'] + r['losses']
        wr = r['wins'] / total * 100 if total else 0
        verdicts.append((label, r['roi'], r['max_dd'], r['balance'], total, r['wins'], r['losses'], wr))

# Also add unconstrained S4 with SL (current live strategy)
s4_sl = sequential_sim(strategies["S4: Unfiltered, Entry $0.30-$0.70"], BANKROLL, 100.0, stop_loss_pct=STOP_LOSS_PCT)
if s4_sl:
    total = s4_sl['wins'] + s4_sl['losses']
    wr = s4_sl['wins'] / total * 100 if total else 0
    verdicts.append(("S4 (unconstrained + SL)", s4_sl['roi'], s4_sl['max_dd'], s4_sl['balance'], total, s4_sl['wins'], s4_sl['losses'], wr))

s6_sl = sequential_sim(strategies["S6: All Signals, Score>=70"], BANKROLL, 100.0, stop_loss_pct=STOP_LOSS_PCT)
if s6_sl:
    total = s6_sl['wins'] + s6_sl['losses']
    wr = s6_sl['wins'] / total * 100 if total else 0
    verdicts.append(("S6 (unconstrained + SL)", s6_sl['roi'], s6_sl['max_dd'], s6_sl['balance'], total, s6_sl['wins'], s6_sl['losses'], wr))

verdicts.sort(key=lambda x: x[1], reverse=True)

output.append(f"\n  Ranked by ROI (all with 70% stop loss):")
output.append(f"  {'#':<4s} {'Strategy':<45s} | {'ROI':>10s} | {'Max DD':>8s} | {'End Bal':>12s} | {'Trades':>7s} | {'Win Rate':>10s}")
output.append(f"  {'-'*110}")
for i, (label, roi, dd, bal, total, w, l, wr) in enumerate(verdicts, 1):
    output.append(
        f"  {i:<4d} {label:<45s} | {roi:>+9.1f}% | {dd:>7.1f}% | ${bal:>10,.2f} | {total:>7d} | {w}W/{l}L ({wr:.0f}%)"
    )

output.append("")
full_output = "\n".join(output) + "\n"

outfile = "edge_roi_stoploss.txt"
with open(outfile, "w", encoding="utf-8") as f:
    f.write(full_output)
print(f"\nResults written to {outfile} ({len(full_output)} bytes)", flush=True)
print("DONE", flush=True)
