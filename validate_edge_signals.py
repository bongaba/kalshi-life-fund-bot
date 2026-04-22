"""Validate edge scanner signals against settlement results and compute ROI."""
import json
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from position_monitor import signed_request

SIGNAL_FILE = "logs/edge_signals/edge_signals_20260410.jsonl"

def load_first_signals():
    """Load signals, grouped by ticker+direction, keeping the first (earliest) signal."""
    signals = []
    with open(SIGNAL_FILE) as f:
        for line in f:
            if line.strip():
                signals.append(json.loads(line.strip()))
    
    first_signals = {}
    for s in signals:
        key = (s["ticker"], s["direction"])
        if key not in first_signals:
            first_signals[key] = s
    return first_signals

def get_settlement(ticker):
    """Query Kalshi API for market settlement result."""
    try:
        data = signed_request("GET", f"/markets/{ticker}")
        market = data.get("market", {})
        status = market.get("status")
        result = market.get("result", "")
        return status, result
    except Exception as e:
        print(f"  ERROR fetching {ticker}: {e}")
        return None, None

def main():
    first_signals = load_first_signals()
    print(f"Total unique ticker+direction combos: {len(first_signals)}")
    
    # Get unique tickers (some have both YES and NO signals)
    unique_tickers = sorted(set(t for t, d in first_signals.keys()))
    print(f"Unique tickers: {len(unique_tickers)}")
    print()
    
    # Query settlement for each ticker
    settlements = {}
    for ticker in unique_tickers:
        status, result = get_settlement(ticker)
        settlements[ticker] = (status, result)
    
    # Calculate results
    results = []
    for (ticker, direction), sig in sorted(first_signals.items()):
        status, result = settlements.get(ticker, (None, None))
        
        # Entry price = best bid on our side at time of signal
        if direction == "YES":
            entry_price = sig.get("yes_best_bid", 0)
        else:
            entry_price = sig.get("no_best_bid", 0)
        
        score = sig["score"]
        
        # Determine win/loss
        if status == "finalized" and result:
            # result is "yes" or "no"
            won = (direction.lower() == result.lower())
            if won:
                pnl_per_contract = 1.00 - entry_price
                roi_pct = (pnl_per_contract / entry_price) * 100 if entry_price > 0 else 0
            else:
                pnl_per_contract = -entry_price
                roi_pct = -100.0
            outcome = "WIN" if won else "LOSS"
        else:
            pnl_per_contract = None
            roi_pct = None
            outcome = f"({status})"
        
        results.append({
            "ticker": ticker,
            "direction": direction,
            "score": score,
            "entry_price": entry_price,
            "status": status,
            "result": result,
            "outcome": outcome,
            "pnl": pnl_per_contract,
            "roi_pct": roi_pct,
        })
    
    # Print detailed results
    print(f"{'Ticker':<42} {'Dir':>3} {'Score':>5} {'Entry':>6} {'Result':>6} {'Outcome':>7} {'PnL':>7} {'ROI%':>7}")
    print("-" * 95)
    
    wins = 0
    losses = 0
    total_pnl = 0
    total_invested = 0
    unsettled = 0
    
    for r in results:
        entry_str = f"${r['entry_price']:.3f}" if r['entry_price'] else "  N/A"
        result_str = r['result'] or "?"
        
        if r['pnl'] is not None:
            pnl_str = f"${r['pnl']:+.3f}"
            roi_str = f"{r['roi_pct']:+.1f}%"
            if r['outcome'] == "WIN":
                wins += 1
            else:
                losses += 1
            total_pnl += r['pnl']
            total_invested += r['entry_price']
        else:
            pnl_str = "  ---"
            roi_str = "  ---"
            unsettled += 1
        
        print(f"{r['ticker']:<42} {r['direction']:>3} {r['score']:>5.1f} {entry_str:>6} {result_str:>6} {r['outcome']:>7} {pnl_str:>7} {roi_str:>7}")
    
    # Summary
    print()
    print("=" * 95)
    total = wins + losses
    if total > 0:
        print(f"SETTLED: {total} trades | {wins}W / {losses}L | Win rate: {wins/total*100:.1f}%")
        print(f"Total PnL (per-contract): ${total_pnl:+.3f}")
        print(f"Total invested (per-contract): ${total_invested:.3f}")
        avg_roi = (total_pnl / total_invested) * 100 if total_invested > 0 else 0
        print(f"Portfolio ROI: {avg_roi:+.1f}%")
        
        # Breakdown by score bucket
        print()
        print("=== By Score Bucket ===")
        buckets = [(60, 70), (70, 80), (80, 90), (90, 101)]
        for lo, hi in buckets:
            bucket = [r for r in results if r['pnl'] is not None and lo <= r['score'] < hi]
            if not bucket:
                continue
            bw = sum(1 for r in bucket if r['outcome'] == "WIN")
            bl = sum(1 for r in bucket if r['outcome'] == "LOSS")
            bpnl = sum(r['pnl'] for r in bucket)
            binv = sum(r['entry_price'] for r in bucket)
            broi = (bpnl / binv) * 100 if binv > 0 else 0
            label = f"{lo}-{hi-1}" if hi <= 100 else f"{lo}-100"
            print(f"  Score {label}: {bw}W/{bl}L ({bw/(bw+bl)*100:.0f}% win) | PnL=${bpnl:+.3f} | ROI={broi:+.1f}%")
        
        # Breakdown by direction
        print()
        print("=== By Direction ===")
        for d in ["YES", "NO"]:
            bucket = [r for r in results if r['pnl'] is not None and r['direction'] == d]
            if not bucket:
                continue
            bw = sum(1 for r in bucket if r['outcome'] == "WIN")
            bl = sum(1 for r in bucket if r['outcome'] == "LOSS")
            bpnl = sum(r['pnl'] for r in bucket)
            binv = sum(r['entry_price'] for r in bucket)
            broi = (bpnl / binv) * 100 if binv > 0 else 0
            print(f"  {d}: {bw}W/{bl}L ({bw/(bw+bl)*100:.0f}% win) | PnL=${bpnl:+.3f} | ROI={broi:+.1f}%")
        
        # Conflicting signals (same ticker, both YES and NO)
        print()
        print("=== Conflicting Signals (both YES and NO on same ticker) ===")
        ticker_dirs = {}
        for r in results:
            if r['pnl'] is not None:
                ticker_dirs.setdefault(r['ticker'], []).append(r)
        conflicts = {t: rs for t, rs in ticker_dirs.items() if len(rs) > 1}
        if conflicts:
            for t, rs in sorted(conflicts.items()):
                dirs = ", ".join(f"{r['direction']}@{r['score']:.0f}={r['outcome']}" for r in rs)
                print(f"  {t}: {dirs}")
        else:
            print("  None")
    
    if unsettled > 0:
        print(f"\nUnsettled: {unsettled} signals")

if __name__ == "__main__":
    main()
