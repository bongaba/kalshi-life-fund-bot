"""
Stop-loss simulation for S4 trades using subsequent signal data as price checks.

Approach:
- Enter at first qualifying signal (unfiltered, $0.30-$0.70, score>=60)
- Track subsequent signals for same ticker as "price snapshots"
- If the price of our side drops by X% from entry → exit at that observed price
- Compare: no stop loss, 15%, 20%, 25%, 30%, 40%, 50% stop losses
- Also test score-based stops: exit if score drops below threshold
"""
import json, os, sys, math, time
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FEE_PER_CONTRACT = 0.02
BANKROLL = 1000.00

# ── Load ALL signals (not deduplicated) ──
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

# Group all signals by ticker (chronological)
by_ticker = defaultdict(list)
for s in signals:
    by_ticker[s["ticker"]].append(s)
for tk in by_ticker:
    by_ticker[tk].sort(key=lambda x: x["ts"])

# ── Load market cache for settlement data ──
MARKET_CACHE_FILE = "logs/edge_signals/_market_cache.json"
cache = {}
if os.path.exists(MARKET_CACHE_FILE):
    with open(MARKET_CACHE_FILE, "r") as f:
        cache = json.load(f)

# ── Build S4 trades with full signal history ──
# Entry: first unfiltered signal per ticker where entry $0.30-$0.70
# Entry: last unfiltered signal per ticker where entry $0.30-$0.70 (matches main analysis)
entries = {}  # ticker -> entry signal
for s in sorted(signals, key=lambda x: x["ts"]):
    tk = s["ticker"]
    if s.get("filtered"):
        continue
    d = s.get("direction", "")
    entry_price = s.get("yes_price", 0) if d == "YES" else s.get("no_price", 0)
    if 0.30 <= entry_price <= 0.70 and s.get("score", 0) >= 60:
        entries[tk] = s  # overwrite with latest = keep last

print(f"S4 entries: {len(entries)}", flush=True)

# Build trade records with subsequent price data
trades = []
for tk, entry_sig in entries.items():
    mkt = cache.get(tk)
    if not mkt:
        continue
    status = mkt.get("status", "")
    result = mkt.get("result", "")
    if status not in ("finalized", "settled"):
        continue

    d = entry_sig["direction"]
    entry_price = entry_sig.get("yes_price", 0) if d == "YES" else entry_sig.get("no_price", 0)
    won = (d == "YES" and result == "yes") or (d == "NO" and result == "no")
    settle_time = mkt.get("settlement_ts") or mkt.get("close_time") or ""

    # Get subsequent signals for this ticker AFTER entry
    subsequent = [s for s in by_ticker[tk] if s["ts"] > entry_sig["ts"]]
    
    # Extract price snapshots for our side
    price_checks = []
    for s in subsequent:
        if d == "YES":
            our_price = s.get("yes_price", 0)
        else:
            our_price = s.get("no_price", 0)
        price_checks.append({
            "ts": s["ts"],
            "price": our_price,
            "score": s.get("score", 0),
        })

    trades.append({
        "ticker": tk,
        "direction": d,
        "score": entry_sig.get("score", 0),
        "entry": entry_price,
        "won": won,
        "settle_time": settle_time,
        "ts": entry_sig["ts"],
        "price_checks": price_checks,
        "n_checks": len(price_checks),
    })

trades.sort(key=lambda x: x["ts"])
has_checks = sum(1 for t in trades if t["n_checks"] > 0)
print(f"Settled S4 trades: {len(trades)} ({has_checks} with subsequent price data)", flush=True)


def simulate_stop_loss(trades, bankroll, risk_per_trade, stop_pct=None, score_stop=None):
    """
    Sequential sim with stop loss.
    stop_pct: exit if our side's price drops by this % from entry (e.g. 0.30 = 30% drop)
    score_stop: exit if score drops below this threshold in subsequent signals
    
    Stop loss exit: sell contracts at observed price (lose entry - exit on each contract + fees)
    """
    def parse_ts(s):
        s = s.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except:
            return datetime.min.replace(tzinfo=timezone.utc)
    
    available = bankroll
    locked = 0.0
    peak_total = bankroll
    max_dd = 0.0
    wins = 0
    losses = 0
    stopped = 0
    stop_saved = 0.0  # money saved by stopping vs holding to loss
    stop_cost = 0.0   # money lost by stopping winners early
    total_fees = 0.0
    open_positions = []

    for t in trades:
        entry_time = parse_ts(t["ts"])
        settle_time_str = t.get("settle_time", "")
        settle_dt = parse_ts(settle_time_str) if settle_time_str else entry_time

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

        alloc = min(risk_per_trade, available)
        contracts = math.floor(alloc / entry_price)
        if contracts <= 0:
            continue

        cost = contracts * entry_price
        fees = contracts * FEE_PER_CONTRACT

        # ── Check for stop loss trigger ──
        triggered_stop = False
        exit_price = None
        
        if (stop_pct is not None or score_stop is not None) and t["price_checks"]:
            for pc in t["price_checks"]:
                # Price-based stop
                if stop_pct is not None:
                    price_drop = (entry_price - pc["price"]) / entry_price
                    if price_drop >= stop_pct:
                        triggered_stop = True
                        exit_price = pc["price"]
                        break
                # Score-based stop
                if score_stop is not None:
                    if pc["score"] < score_stop:
                        triggered_stop = True
                        exit_price = pc["price"]
                        break

        if triggered_stop and exit_price is not None:
            # Stop loss exit: sell at observed price
            # P&L = (exit_price - entry_price) * contracts - fees (both entry + exit)
            exit_fees = contracts * FEE_PER_CONTRACT
            gross = (exit_price - entry_price) * contracts
            net = gross - fees - exit_fees
            total_fees += fees + exit_fees
            stopped += 1
            
            # Track what would have happened without the stop
            if t["won"]:
                # We stopped a winner — cost = what we missed
                would_have_net = contracts * (1.0 - entry_price) - fees
                stop_cost += (would_have_net - net)
            else:
                # We stopped a loser — saved = loss avoided
                would_have_net = -(contracts * entry_price) - fees
                stop_saved += (net - would_have_net)

            if net > 0:
                wins += 1
            else:
                losses += 1
            
            # Capital returns immediately (we exited)
            available -= cost
            available += cost + net
            
        else:
            # No stop triggered — hold to settlement
            if t["won"]:
                gross = contracts * (1.0 - entry_price)
                wins += 1
            else:
                gross = -(contracts * entry_price)
                losses += 1
            net = gross - fees
            total_fees += fees

            # Lock capital until settlement
            available -= cost
            locked += cost
            open_positions.append({
                "settle_dt": settle_dt,
                "cost": cost,
                "pnl": net,
            })

        # Track drawdown
        total_balance = available + locked + sum(p["pnl"] for p in open_positions)
        if total_balance > peak_total:
            peak_total = total_balance
        dd = (peak_total - total_balance) / peak_total * 100 if peak_total > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # Settle remaining
    for pos in open_positions:
        available += pos["cost"] + pos["pnl"]
    
    final = available
    roi = (final - bankroll) / bankroll * 100

    return {
        "roi": roi,
        "balance": final,
        "max_dd": max_dd,
        "wins": wins,
        "losses": losses,
        "stopped": stopped,
        "stop_saved": stop_saved,
        "stop_cost": stop_cost,
        "total_fees": total_fees,
        "n": wins + losses,
    }


# ── Run simulations ──
output = []
output.append("=" * 110)
output.append("  STOP LOSS SIMULATION — S4 Strategy (Unfiltered, Entry $0.30-$0.70)")
output.append("  Using subsequent signal snapshots as price checks")
output.append(f"  Bankroll: ${BANKROLL:,.0f} | Risk/trade: $100 | Trades with price data: {has_checks}/{len(trades)}")
output.append("=" * 110)
output.append("")

# Price-based stop losses
output.append("  PRICE-BASED STOP LOSSES")
output.append("  Exit if our side's price drops by X% from entry price")
output.append(f"  {'Stop Level':<25s} | {'ROI':>9s} | {'End Bal':>11s} | {'Max DD':>7s} | {'W/L':>16s} | {'Stopped':>8s} | {'$ Saved':>10s} | {'$ Cost':>10s} | {'Net Impact':>11s}")
output.append(f"  {'-'*120}")

# No stop loss (baseline)
base = simulate_stop_loss(trades, BANKROLL, 100.0, stop_pct=None)
output.append(f"  {'No Stop Loss (baseline)':<25s} | {base['roi']:>+8.1f}% | ${base['balance']:>9,.2f} | {base['max_dd']:>6.1f}% | {base['wins']}W/{base['losses']}L ({base['wins']/(base['n'])*100:.0f}%) | {'--':>8s} | {'--':>10s} | {'--':>10s} | {'--':>11s}")

for pct in [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.80]:
    r = simulate_stop_loss(trades, BANKROLL, 100.0, stop_pct=pct)
    total = r['wins'] + r['losses']
    wr = r['wins'] / total * 100 if total else 0
    net_impact = r['stop_saved'] - r['stop_cost']
    roi_diff = r['roi'] - base['roi']
    output.append(f"  {'Stop at -' + f'{pct*100:.0f}%':<25s} | {r['roi']:>+8.1f}% | ${r['balance']:>9,.2f} | {r['max_dd']:>6.1f}% | {r['wins']}W/{r['losses']}L ({wr:.0f}%) | {r['stopped']:>8d} | ${r['stop_saved']:>8,.2f} | ${r['stop_cost']:>8,.2f} | ${net_impact:>+9,.2f}")

output.append("")
output.append("")

# Score-based stop losses
output.append("  SCORE-BASED STOP LOSSES")
output.append("  Exit if subsequent signal score drops below threshold")
output.append(f"  {'Stop Level':<25s} | {'ROI':>9s} | {'End Bal':>11s} | {'Max DD':>7s} | {'W/L':>16s} | {'Stopped':>8s} | {'$ Saved':>10s} | {'$ Cost':>10s} | {'Net Impact':>11s}")
output.append(f"  {'-'*120}")

output.append(f"  {'No Stop Loss (baseline)':<25s} | {base['roi']:>+8.1f}% | ${base['balance']:>9,.2f} | {base['max_dd']:>6.1f}% | {base['wins']}W/{base['losses']}L ({base['wins']/(base['n'])*100:.0f}%) | {'--':>8s} | {'--':>10s} | {'--':>10s} | {'--':>11s}")

for score_th in [55, 50, 45, 40, 35, 30]:
    r = simulate_stop_loss(trades, BANKROLL, 100.0, score_stop=score_th)
    total = r['wins'] + r['losses']
    wr = r['wins'] / total * 100 if total else 0
    net_impact = r['stop_saved'] - r['stop_cost']
    output.append(f"  {'Score < ' + str(score_th):<25s} | {r['roi']:>+8.1f}% | ${r['balance']:>9,.2f} | {r['max_dd']:>6.1f}% | {r['wins']}W/{r['losses']}L ({wr:.0f}%) | {r['stopped']:>8d} | ${r['stop_saved']:>8,.2f} | ${r['stop_cost']:>8,.2f} | ${net_impact:>+9,.2f}")

output.append("")
output.append("")

# Combined: price + score stops
output.append("  COMBINED STOP LOSSES (price OR score trigger)")
output.append(f"  {'Stop Level':<35s} | {'ROI':>9s} | {'End Bal':>11s} | {'Max DD':>7s} | {'W/L':>16s} | {'Stopped':>8s} | {'Net Impact':>11s}")
output.append(f"  {'-'*110}")

combos = [
    (0.20, 50, "-20% price OR score<50"),
    (0.25, 50, "-25% price OR score<50"),
    (0.30, 45, "-30% price OR score<45"),
    (0.30, 40, "-30% price OR score<40"),
    (0.25, 45, "-25% price OR score<45"),
    (0.20, 45, "-20% price OR score<45"),
]
for price_pct, score_th, label in combos:
    r = simulate_stop_loss(trades, BANKROLL, 100.0, stop_pct=price_pct, score_stop=score_th)
    total = r['wins'] + r['losses']
    wr = r['wins'] / total * 100 if total else 0
    net_impact = r['stop_saved'] - r['stop_cost']
    output.append(f"  {label:<35s} | {r['roi']:>+8.1f}% | ${r['balance']:>9,.2f} | {r['max_dd']:>6.1f}% | {r['wins']}W/{r['losses']}L ({wr:.0f}%) | {r['stopped']:>8d} | ${net_impact:>+9,.2f}")

output.append("")

# Detail: what happened to stopped trades
output.append("")
output.append("  STOP LOSS DETAIL — Baseline (no stop) vs Best Price Stop")
output.append("  Showing individual trade outcomes for stopped trades at -20%")
output.append(f"  {'Ticker':<40s} | {'Dir':>3s} | {'Entry':>6s} | {'Exit':>6s} | {'Drop':>6s} | {'Settlement':>10s} | {'Action':>12s}")
output.append(f"  {'-'*100}")

# Re-run -20% to get individual trade details
for t in trades:
    if not t["price_checks"]:
        continue
    entry_price = t["entry"]
    for pc in t["price_checks"]:
        price_drop = (entry_price - pc["price"]) / entry_price
        if price_drop >= 0.20:
            outcome = "WIN" if t["won"] else "LOSS"
            action = "SAVED" if not t["won"] else "MISSED WIN"
            output.append(f"  {t['ticker']:<40s} | {t['direction']:>3s} | ${entry_price:.2f} | ${pc['price']:.2f} | {price_drop*100:>5.1f}% | {outcome:>10s} | {action:>12s}")
            break

output.append("")

full = "\n".join(output) + "\n"
outfile = "edge_stop_loss_sim.txt"
with open(outfile, "w", encoding="utf-8") as f:
    f.write(full)
print(f"\nResults written to {outfile} ({len(full)} bytes)", flush=True)

# Also print the key tables to console
for line in output:
    print(line, flush=True)

print("\nDONE", flush=True)
