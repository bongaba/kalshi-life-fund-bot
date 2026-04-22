import sqlite3
conn = sqlite3.connect('trades.db')
cur = conn.cursor()
cur.execute("""
    SELECT id, market_ticker, direction, size, price, status, pnl, timestamp
    FROM trades WHERE reason LIKE 'edge_scanner%'
    ORDER BY id DESC LIMIT 20
""")
for r in cur.fetchall():
    print(r)
print("---")
cur.execute("""
    SELECT status, COUNT(*) FROM trades WHERE reason LIKE 'edge_scanner%' GROUP BY status
""")
for r in cur.fetchall():
    print(r)
conn.close()
