import os, glob

log_dir = "logs/monitor"
log_files = sorted(glob.glob(os.path.join(log_dir, "*.log")), key=os.path.getmtime, reverse=True)

# Recent losing tickers
losers = [
    ("1443", "KXBTC15M-26APR110015-15", "YES", 0.988),   # BTC 15M, Apr 11 04:11
    ("1435", "KXBTCD-26APR1023-T72999.99", "YES", 0.59),  # BTC daily, Apr 11 02:45
    ("1425/1426", "KXBTCD-26APR1021-T72899.99", "YES", 0.67),  # BTC daily, Apr 11 00:42
    ("1411/1412/1413", "KXBTCD-26APR1016-T73199.99", "NO", 0.94),  # BTC daily, Apr 10 19:43
    ("1375", "KXBTC15M-26APR100830-30", "YES", 0.49),     # BTC 15M, Apr 10 12:24
    ("1371/1372", "KXBTC15M-26APR100615-15", "NO", 0.53), # BTC 15M, Apr 10 10:13
]

for trade_id, ticker, direction, entry in losers:
    print(f"\n{'='*80}")
    print(f"TRADE {trade_id}: {ticker} | {direction} @ ${entry}")
    print(f"{'='*80}")
    
    found_any = False
    for lf in log_files[:10]:  # check last 10 log files
        with open(lf, 'r', errors='ignore') as f:
            pnl_lines = []
            sl_lines = []
            skip_lines = []
            blocked_lines = []
            for line in f:
                if ticker not in line:
                    continue
                if 'pnl_pct' in line:
                    pnl_lines.append(line.strip())
                if 'stop' in line.lower() or 'breach' in line.lower() or 'EXIT' in line:
                    sl_lines.append(line.strip())
                if 'Skipping' in line or 'no real-time quote' in line or 'no_yes_bids' in line or 'no_no_bids' in line:
                    skip_lines.append(line.strip())
                if 'blocked' in line.lower() or 'holding' in line.lower() or 'low_liquidity' in line.lower():
                    blocked_lines.append(line.strip())
            
            if pnl_lines or sl_lines or skip_lines or blocked_lines:
                found_any = True
                print(f"\n  --- {os.path.basename(lf)} ---")
                if pnl_lines:
                    print(f"  First PnL: {pnl_lines[0][:200]}")
                    print(f"  Last PnL:  {pnl_lines[-1][:200]}")
                    print(f"  Total PnL readings: {len(pnl_lines)}")
                if sl_lines:
                    print(f"  SL events ({len(sl_lines)}):")
                    for s in sl_lines[:5]:
                        print(f"    {s[:200]}")
                    if len(sl_lines) > 5:
                        print(f"    ... and {len(sl_lines)-5} more")
                        for s in sl_lines[-3:]:
                            print(f"    {s[:200]}")
                if skip_lines:
                    print(f"  SKIPPED (no quote): {len(skip_lines)} times")
                    print(f"    First: {skip_lines[0][:200]}")
                    print(f"    Last:  {skip_lines[-1][:200]}")
                if blocked_lines:
                    print(f"  BLOCKED ({len(blocked_lines)}):")
                    for b in blocked_lines[:5]:
                        print(f"    {b[:200]}")
    
    if not found_any:
        print("  *** NO MONITOR LOGS FOUND FOR THIS TICKER ***")
