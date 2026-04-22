"""Simulate $1000 bankroll divided evenly across all settled edge scanner trades."""
import json
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from position_monitor import signed_request

BANKROLL = 1000.0

def main():
    signals = []
    with open("logs/edge_signals/edge_signals_20260410.jsonl") as f:
        for line in f:
            if line.strip():
                signals.append(json.loads(line.strip()))

    first_signals = {}
    for s in signals:
        key = (s["ticker"], s["direction"])
        if key not in first_signals:
            first_signals[key] = s

    unique_tickers = sorted(set(t for t, d in first_signals.keys()))
    settlements = {}
    for ticker in unique_tickers:
        try:
            data = signed_request("GET", f"/markets/{ticker}")
            m = data.get("market", {})
            settlements[ticker] = (m.get("status"), m.get("result", ""))
        except:
            settlements[ticker] = (None, None)

    settled = []
    for (ticker, direction), sig in sorted(first_signals.items()):
        status, result = settlements.get(ticker, (None, None))
        if status != "finalized" or not result:
            continue
        entry = sig.get("yes_best_bid", 0) if direction == "YES" else sig.get("no_best_bid", 0)
        won = direction.lower() == result.lower()
        settled.append({"ticker": ticker, "dir": direction, "score": sig["score"], "entry": entry, "won": won})

    def simulate(label, trades):
        if not trades:
            print(f"\n{label}: No trades")
            return
        n = len(trades)
        alloc = BANKROLL / n
        print(f"\n{'='*80}")
        print(f"  {label}")
        print(f"  Bankroll: ${BANKROLL:,.0f} | Trades: {n} | Per trade: ${alloc:,.2f}")
        print(f"{'='*80}")
        print(f"  {'Ticker':<40} {'Dir':>3} {'Scr':>4} {'Entry':>6} {'Ctrs':>5} {'W/L':>4} {'Gross':>9} {'Fees':>6} {'Net':>9}")
        print(f"  {'-'*90}")

        total_gross = 0
        total_fees = 0
        wins = 0
        losses = 0
        total_cost = 0

        for t in trades:
            contracts = int(alloc / t["entry"])
            cost = contracts * t["entry"]
            fee = contracts * 0.02  # 2c/contract entry fee

            if t["won"]:
                gross = contracts * 1.00 - cost
                wins += 1
            else:
                gross = -cost
                losses += 1

            net = gross - fee
            total_gross += gross
            total_fees += fee
            total_cost += cost
            wl = "WIN" if t["won"] else "LOSS"
            print(f"  {t['ticker']:<40} {t['dir']:>3} {t['score']:>4.0f} ${t['entry']:.3f} {contracts:>5} {wl:>4} ${gross:>+8.2f} ${fee:>5.2f} ${net:>+8.2f}")

        total_net = total_gross - total_fees
        print(f"  {'-'*90}")
        print(f"  GROSS PROFIT: ${total_gross:>+10.2f}")
        print(f"  TOTAL FEES:   ${total_fees:>10.2f}")
        print(f"  NET PROFIT:   ${total_net:>+10.2f}")
        print(f"  NET ROI:      {(total_net / BANKROLL) * 100:>+.1f}%")
        print(f"  Record:       {wins}W / {losses}L ({wins/(wins+losses)*100:.0f}% win rate)")

    # === Strategy 1: All signals ===
    simulate("STRATEGY 1: ALL SIGNALS (score >= 60)", settled)

    # === Strategy 2: Score >= 70 ===
    settled70 = [t for t in settled if t["score"] >= 70]
    simulate("STRATEGY 2: SCORE >= 70", settled70)

    # === Strategy 3: Score >= 70, no conflicts ===
    ticker_dirs70 = {}
    for t in settled:
        if t["score"] >= 70:
            ticker_dirs70.setdefault(t["ticker"], []).append(t)
    no_conflict = [ts[0] for ts in ticker_dirs70.values() if len(ts) == 1]
    simulate("STRATEGY 3: SCORE >= 70, NO CONFLICTS (skip if both sides signal)", sorted(no_conflict, key=lambda x: x["ticker"]))

    # === Strategy 4: Take higher-scoring side only ===
    all_by_ticker = {}
    for t in settled:
        all_by_ticker.setdefault(t["ticker"], []).append(t)
    best_side = []
    for ticker, trades in all_by_ticker.items():
        best = max(trades, key=lambda x: x["score"])
        if best["score"] >= 70:
            best_side.append(best)
    simulate("STRATEGY 4: SCORE >= 70, TAKE HIGHER-SCORING SIDE ONLY", sorted(best_side, key=lambda x: x["ticker"]))

    # === Strategy 5: Score >= 80 ===
    settled80 = [t for t in settled if t["score"] >= 80]
    simulate("STRATEGY 5: SCORE >= 80", settled80)

if __name__ == "__main__":
    main()
