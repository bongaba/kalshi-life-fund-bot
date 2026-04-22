import sqlite3, json, time, base64, requests, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import MODE, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

host = "https://api.elections.kalshi.com" if MODE == "live" else "https://demo-api.kalshi.co"
api_prefix = "/trade-api/v2"
with open(KALSHI_PRIVATE_KEY_PATH, "rb") as f:
    private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def signed_get(path):
    ts = str(int(time.time() * 1000))
    full = api_prefix + path
    msg = f"{ts}GET{full}".encode()
    sig = private_key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    h = {"KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID, "KALSHI-ACCESS-TIMESTAMP": ts, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}
    return requests.get(host + full, headers=h, timeout=10).json()

# Check market settlement
ticker = "KXBTCD-26APR1119-T73299.99"
mkt = signed_get(f"/markets/{ticker}").get("market", {})
print(f"Market: {ticker}")
print(f"  Status: {mkt.get('status')}")
print(f"  Result: {mkt.get('result')}")
print(f"  Close time: {mkt.get('close_time')}")
print(f"  Expiration: {mkt.get('expiration_time')}")
print()

# All DB rows for this ticker
conn = sqlite3.connect('trades.db')
rows = conn.execute("SELECT * FROM trades WHERE market_ticker = ?", (ticker,)).fetchall()
cols = [d[0] for d in conn.execute("PRAGMA table_info(trades)").fetchall()]
col_names = [c[1] for c in conn.execute("PRAGMA table_info(trades)").fetchall()]
for r in rows:
    print(f"Row #{r[0]}:")
    for i, v in enumerate(r):
        if v is not None and v != 0:
            print(f"  {col_names[i]}: {v}")
    print()
conn.close()
