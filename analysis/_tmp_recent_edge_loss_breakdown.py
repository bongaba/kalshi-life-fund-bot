import sqlite3
from datetime import datetime, timedelta, timezone

conn = sqlite3.connect('trades.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Find edge trades from recent days (uses timestamp text in db)
cur.execute(
    """
    SELECT id, market_ticker, direction, status, pnl, reason, timestamp
    FROM trades
    WHERE reason LIKE '%edge%'
      AND status IN ('WON','LOST','CLOSED')
    ORDER BY timestamp DESC
    LIMIT 800
    """
)
rows = cur.fetchall()

print(f"recent edge rows inspected: {len(rows)}")

# PnL rollups by date
by_date = {}
for r in rows:
    ts = (r['timestamp'] or '')[:10]
    if not ts:
        continue
    d = by_date.setdefault(ts, {'trades': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0})
    d['trades'] += 1
    if r['status'] == 'WON' or (r['pnl'] or 0) > 0:
        d['wins'] += 1
    elif r['status'] == 'LOST' or (r['pnl'] or 0) < 0:
        d['losses'] += 1
    d['pnl'] += float(r['pnl'] or 0)

print('\nRecent edge performance by day:')
for day in sorted(by_date.keys(), reverse=True)[:7]:
    d = by_date[day]
    wr = (d['wins'] / d['trades'] * 100) if d['trades'] else 0
    print(f"  {day}: trades={d['trades']}, W/L={d['wins']}/{d['losses']} ({wr:.0f}%), pnl=${d['pnl']:.2f}")

# Biggest losses (to inspect pattern)
print('\nTop 15 worst edge losses:')
losses = [r for r in rows if float(r['pnl'] or 0) < 0]
losses.sort(key=lambda r: float(r['pnl']))
for r in losses[:15]:
    print(f"  {r['timestamp']} | {r['market_ticker']} | {r['direction']} | status={r['status']} | pnl=${float(r['pnl']):.2f}")

# Recent streak snapshot (last 100)
last100 = rows[:100]
wins100 = sum(1 for r in last100 if (r['status'] == 'WON' or float(r['pnl'] or 0) > 0))
loss100 = sum(1 for r in last100 if (r['status'] == 'LOST' or float(r['pnl'] or 0) < 0))
pnl100 = sum(float(r['pnl'] or 0) for r in last100)
print('\nLast 100 edge trades:')
print(f"  W/L={wins100}/{loss100}, winrate={(wins100/len(last100)*100 if last100 else 0):.1f}%, pnl=${pnl100:.2f}")

conn.close()
