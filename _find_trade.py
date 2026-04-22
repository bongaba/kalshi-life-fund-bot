import sqlite3
conn = sqlite3.connect('trades.db')
c = conn.cursor()
c.execute("""SELECT id, timestamp, market_ticker, direction, size, price, size*price as cost, status, pnl, event_ticker
FROM trades 
WHERE market_ticker LIKE '%KXBTC15M%' 
AND timestamp LIKE '2026-04-11%'
ORDER BY timestamp ASC
LIMIT 30""")
for r in c.fetchall():
    print(r)
conn.close()
