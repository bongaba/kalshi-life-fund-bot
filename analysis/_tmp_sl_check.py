import sqlite3
conn = sqlite3.connect('trades.db')
cur = conn.cursor()
cur.execute("""SELECT market_ticker, direction, price, size, status, pnl, reason, timestamp, fees
FROM trades WHERE reason LIKE 'edge_scanner%' AND status != 'OPEN'
ORDER BY timestamp DESC LIMIT 20""")
for r in cur.fetchall():
    print(f"{r[7]} | {r[0]} | dir={r[1]} | entry=${r[2]} | size={r[3]} | pnl=${r[5]} | fees=${r[8]} | status={r[4]} | {r[6][:50]}")
