import time, asyncio, threading
from orderbook_edge_scanner import (
    KalshiWebSocketClient, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH,
    BOOKS, QUOTES, _BOOK_LOCK, handle_orderbook
)

tickers = ['KXBTCD-26APR2017-T74999.99', 'KXBTCD-26APR2017-T75249.99']

client = KalshiWebSocketClient(KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, set(tickers), handle_orderbook)

def run():
    asyncio.run(client.connect())

t = threading.Thread(target=run, daemon=True)
t.start()
time.sleep(8)

with _BOOK_LOCK:
    for tk in tickers:
        b = BOOKS.get(tk)
        q = QUOTES.get(tk)
        print(f"{tk}: book={bool(b)}, quote={bool(q)}")
        if b:
            yes_book = b.get("yes", {})
            no_book = b.get("no", {})
            print(f"  yes_levels={len(yes_book)} no_levels={len(no_book)}")
            if yes_book:
                best = max(yes_book.keys())
                print(f"  yes_best_bid=${best:.3f} size={yes_book[best]}")
            if no_book:
                best = max(no_book.keys())
                print(f"  no_best_bid=${best:.3f} size={no_book[best]}")
        if q:
            age = (time.time() - q["timestamp"].timestamp())
            print(f"  quote_age={age:.1f}s")
        else:
            print("  NO QUOTE DATA")
