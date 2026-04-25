import sqlite3
conn = sqlite3.connect('trades.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("SELECT * FROM trades WHERE status = 'OPEN' ORDER BY timestamp DESC")
rows = cur.fetchall()
for r in rows:
    d = dict(r)
    print(d)
    print("---")
if not rows:
    print("No open trades found. Showing last 5 trades:")
    cur.execute("SELECT * FROM trades ORDER BY timestamp DESC LIMIT 5")
    for r in cur.fetchall():
        print(dict(r))
        print("---")
conn.close()
