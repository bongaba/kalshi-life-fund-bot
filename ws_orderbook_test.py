"""
ws_orderbook_test.py — Connect to Kalshi WebSocket and print live orderbook flow.

Usage:
    python ws_orderbook_test.py                        # auto-discover tickers from open positions + soonest-closing markets
    python ws_orderbook_test.py KXBTC-26MAR-B87000     # subscribe to specific ticker(s)
    python ws_orderbook_test.py KXBTC-26MAR-B87000 KXBTCD-26MAR-T88000

Press Ctrl+C to stop.
"""
import asyncio
import sys
import os
import time
import json
import base64
import requests
from datetime import datetime, timezone
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

# Load config
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH

KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
API_HOST = "https://api.elections.kalshi.com"
API_PREFIX = "/trade-api/v2"

# Load private key
with open(KALSHI_PRIVATE_KEY_PATH, "rb") as f:
    PRIVATE_KEY = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

with open(KALSHI_PRIVATE_KEY_PATH, "r") as f:
    PRIVATE_KEY_PEM = f.read()


def signed_get(path):
    ts = str(int(time.time() * 1000))
    msg = ts + "GET" + API_PREFIX + path
    sig = PRIVATE_KEY.sign(
        msg.encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256()
    )
    headers = {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }
    return requests.get(API_HOST + API_PREFIX + path, headers=headers).json()


def discover_tickers():
    """Find tickers from open positions + soonest-closing markets."""
    tickers = set()

    # From open positions
    try:
        data = signed_get("/portfolio/positions")
        for p in data.get("market_positions", []):
            t = p.get("ticker")
            if t:
                tickers.add(t)
                print(f"  [position] {t}")
    except Exception as e:
        print(f"  Could not fetch positions: {e}")

    # From soonest-closing open markets (most likely to have flow)
    try:
        data = signed_get("/markets?status=open&limit=200")
        markets = data.get("markets", [])

        def parse_close(m):
            ct = m.get("close_time", "")
            try:
                return datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
            except:
                return 9999999999

        markets.sort(key=parse_close)
        # Take up to 5 soonest-closing markets
        for m in markets[:5]:
            t = m.get("ticker")
            if t:
                tickers.add(t)
                ct = (m.get("close_time") or "?")[:19]
                print(f"  [soonest] {t} close={ct} vol={m.get('volume', 0)}")
    except Exception as e:
        print(f"  Could not fetch markets: {e}")

    return tickers


async def run(tickers):
    import websockets

    def make_auth_headers():
        method = "GET"
        path = "/trade-api/ws/v2"
        timestamp = str(int(time.time() * 1000))
        msg_string = timestamp + method + path
        pk = serialization.load_pem_private_key(PRIVATE_KEY_PEM.encode(), password=None)
        signature = pk.sign(
            msg_string.encode("utf-8"),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return [
            ("Content-Type", "application/json"),
            ("KALSHI-ACCESS-KEY", KALSHI_API_KEY_ID),
            ("KALSHI-ACCESS-SIGNATURE", base64.b64encode(signature).decode()),
            ("KALSHI-ACCESS-TIMESTAMP", timestamp),
        ]

    # Maintain local orderbook per ticker
    books = {}  # ticker -> {"yes": {price: size}, "no": {price: size}, "seq": int}

    print(f"\n{'='*80}")
    print(f"Connecting to {KALSHI_WS_URL}...")
    print(f"Subscribing to {len(tickers)} ticker(s): {sorted(tickers)}")
    print(f"{'='*80}\n")

    async with websockets.connect(KALSHI_WS_URL, additional_headers=make_auth_headers()) as ws:
        print("[CONNECTED]\n")

        # Subscribe to all tickers
        for ticker in tickers:
            sub = {
                "id": int(time.time() * 1000),
                "cmd": "subscribe",
                "params": {"channels": ["orderbook_delta"], "market_ticker": ticker},
            }
            await ws.send(json.dumps(sub))
            print(f"[SUB] {ticker}")

        print(f"\nListening for orderbook updates... (Ctrl+C to stop)\n")
        msg_count = 0

        async for raw in ws:
            msg_count += 1
            try:
                data = json.loads(raw)
                msg_type = data.get("type", "")
                ts_now = datetime.now().strftime("%H:%M:%S.%f")[:-3]

                if msg_type in ("orderbook_snapshot", "orderbook_delta"):
                    inner = data.get("msg", {})
                    ticker = inner.get("market_ticker", "unknown")
                    seq = inner.get("seq", "?")
                    yes_levels = inner.get("yes", [])
                    no_levels = inner.get("no", [])
                    tag = "SNAP" if msg_type == "orderbook_snapshot" else "DLTA"

                    # Update local book
                    if msg_type == "orderbook_snapshot":
                        books[ticker] = {
                            "yes": {float(p): float(s) for p, s in yes_levels} if yes_levels else {},
                            "no": {float(p): float(s) for p, s in no_levels} if no_levels else {},
                            "seq": seq,
                        }
                    else:
                        book = books.get(ticker)
                        if book is None:
                            print(f"  {ts_now} [{tag}] {ticker} seq={seq} *** DELTA BEFORE SNAPSHOT — SKIPPED ***")
                            continue
                        for p, s in (yes_levels or []):
                            pf, sf = float(p), float(s)
                            if sf <= 0:
                                book["yes"].pop(pf, None)
                            else:
                                book["yes"][pf] = sf
                        for p, s in (no_levels or []):
                            pf, sf = float(p), float(s)
                            if sf <= 0:
                                book["no"].pop(pf, None)
                            else:
                                book["no"][pf] = sf
                        book["seq"] = seq

                    # Display
                    book = books[ticker]
                    yes_sorted = sorted(book["yes"].items())
                    no_sorted = sorted(book["no"].items())
                    yes_best = yes_sorted[-1] if yes_sorted else None
                    no_best = no_sorted[-1] if no_sorted else None

                    # Changes summary
                    changes = []
                    for p, s in (yes_levels or []):
                        action = "DEL" if float(s) <= 0 else "SET"
                        changes.append(f"Y${float(p):.2f}={float(s):.0f}({action})")
                    for p, s in (no_levels or []):
                        action = "DEL" if float(s) <= 0 else "SET"
                        changes.append(f"N${float(p):.2f}={float(s):.0f}({action})")

                    line = f"  {ts_now} [{tag}] {ticker} seq={seq}"
                    if yes_best:
                        line += f" | YES best=${yes_best[0]:.2f}x{yes_best[1]:.0f}"
                    if no_best:
                        line += f" | NO best=${no_best[0]:.2f}x{no_best[1]:.0f}"
                    line += f" | depth=Y{len(yes_sorted)}/N{len(no_sorted)}"
                    if changes:
                        line += f" | {', '.join(changes)}"
                    print(line)

                    # Print full book every 10th snapshot
                    if msg_type == "orderbook_snapshot" and yes_sorted:
                        print(f"    YES book: {['${:.2f}x{:.0f}'.format(p,s) for p,s in yes_sorted]}")
                        print(f"    NO  book: {['${:.2f}x{:.0f}'.format(p,s) for p,s in no_sorted]}")

                elif msg_type == "error":
                    print(f"  {ts_now} [ERROR] {data}")
                else:
                    # Show other message types (subscribed confirmations, etc.)
                    print(f"  {ts_now} [OTHER type={msg_type}] {json.dumps(data)[:200]}")

            except Exception as e:
                print(f"  [PARSE ERROR] {e}: {raw[:200]}")


def main():
    if len(sys.argv) > 1:
        tickers = set(sys.argv[1:])
        print(f"Using command-line tickers: {sorted(tickers)}")
    else:
        print("Auto-discovering tickers...")
        tickers = discover_tickers()

    if not tickers:
        print("\nNo tickers found. Pass ticker(s) as arguments:")
        print("  python ws_orderbook_test.py KXBTC-26MAR-B87000")
        return

    try:
        asyncio.run(run(tickers))
    except KeyboardInterrupt:
        print("\n\n[STOPPED]")


if __name__ == "__main__":
    main()
