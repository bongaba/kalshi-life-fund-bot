import sqlite3

conn = sqlite3.connect('trades.db')
try:
    conn.execute("ALTER TABLE trades ADD COLUMN status TEXT DEFAULT 'OPEN'")
    print('Added status column.')
except sqlite3.OperationalError as e:
    print(f'Error: {e}')
conn.commit()
conn.close()