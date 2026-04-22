import sqlite3
conn = sqlite3.connect('trades.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("SELECT * FROM trades WHERE status = 'LOST' ORDER BY timestamp DESC LIMIT 10")
for r in cur.fetchall():
    d = dict(r)
    print(f"id={d['id']} | {d['timestamp']} | {d['market_ticker']}")
    print(f"  dir={d['direction']} | entry=${d['price']} | size=${d['size']} | pnl=${d['pnl']} | fees={d['fees']}")
    print(f"  reason={d['reason']}")
    print(f"  resolved={d['resolved_timestamp']}")
    print('---')
conn.close()
