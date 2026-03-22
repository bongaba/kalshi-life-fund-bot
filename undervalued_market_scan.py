import argparse
import csv
import json
import time
from datetime import datetime

import requests
from xai_sdk.chat import user
from xai_sdk.tools import web_search, x_search

from config import MODE, OPEN_MARKETS_MAX_PAGES
from grok_analyzer import get_grok_client, parse_first_json_object


# ========================= CONFIG (you can tweak these) =========================
MIN_EDGE_DEFAULT = 0.085
MIN_VOLUME_DEFAULT = 25000
SCAN_INTERVAL_SECONDS_DEFAULT = 300
MAX_MARKETS_TO_ANALYZE_DEFAULT = 50
BATCH_SIZE_DEFAULT = 8
# =============================================================================


HOST = "https://demo-api.kalshi.co" if MODE == "demo" else "https://api.elections.kalshi.com"


def to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_price(value):
    price = to_float(value, default=0.0)
    if price > 1:
        price /= 100.0
    return max(0.0, min(1.0, price))


def get_volume(market):
    return max(
        to_float(market.get("volume"), 0.0),
        to_float(market.get("volume_fp"), 0.0),
        to_float(market.get("volume_24h"), 0.0),
        to_float(market.get("volume_24h_fp"), 0.0),
    )


def fetch_active_kalshi_markets(max_pages=1, per_page_limit=500):
    """Pull all open markets using cursor pagination."""
    url = f"{HOST}/trade-api/v2/markets"
    all_markets = []
    cursor = None
    page = 1

    while page <= max_pages:
        params = {
            "status": "open",
            "limit": per_page_limit,
        }
        if cursor:
            params["cursor"] = cursor

        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            payload = resp.json()
            markets = payload.get("markets", [])
            all_markets.extend(markets)
            cursor = payload.get("cursor")
            if not cursor:
                break
            page += 1
        except requests.RequestException as error:
            print(f"API error: {error}")
            break

    return all_markets


def get_yes_price(market):
    """Get current Yes price (midpoint)."""
    bid = normalize_price(market.get("yes_bid_dollars") or market.get("yes_bid"))
    ask = normalize_price(market.get("yes_ask_dollars") or market.get("yes_ask"))
    last = normalize_price(market.get("last_price_dollars") or market.get("last_price") or 0.5)
    return (bid + ask) / 2 if bid > 0 and ask > 0 else last


def get_no_price(market, yes_price):
    """Get current NO price (midpoint when available, otherwise complement of YES)."""
    bid = normalize_price(market.get("no_bid_dollars") or market.get("no_bid"))
    ask = normalize_price(market.get("no_ask_dollars") or market.get("no_ask"))

    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return max(0.0, min(1.0, 1.0 - yes_price))


def get_ai_probabilities_batch(markets_batch, demo_ai=False):
    """One Grok call for up to N markets using the existing Grok project setup."""
    prompt = "Analyze these Kalshi markets and estimate the TRUE probability each resolves YES. Use fresh web_search and x_search results before answering. Respond in JSON only.\n"
    for i, market in enumerate(markets_batch):
        prompt += (
            f'{i + 1}. Market: "{market.get("title", "No title")}" | '
            f'Ticker: {market.get("ticker")} | Current Yes price: ${get_yes_price(market):.2f}\n'
        )

    prompt += (
        "\nRequirements:\n"
        "- You MUST use BOTH web_search and x_search before answering.\n"
        "- Base every estimate on fresh, source-backed information only.\n"
        "- If uncertain, use lower confidence and a probability near the market price rather than guessing.\n"
        "- Return percentages, not decimals.\n"
        "\nReturn ONLY this JSON:\n"
        '{"0": {"prob": 78, "conf": 92}, "1": {"prob": 41, "conf": 75}}'
    )

    print(f"   ONE AI call analyzing {len(markets_batch)} markets...")

    if demo_ai:
        return {str(i): {"prob": 79, "conf": 89} for i in range(len(markets_batch))}

    client = get_grok_client()
    if client is None:
        raise RuntimeError("Grok client is not configured. Check XAI_API_KEY in .env.")

    chat = client.chat.create(
        model="grok-4-1-fast-reasoning",
        messages=[user(prompt)],
        tools=[web_search(), x_search()],
        tool_choice="required",
        temperature=0.1,
        max_tokens=600,
        max_turns=6,
    )
    response = chat.sample()
    raw_text = str(response.content).strip()

    parsed, _ = parse_first_json_object(raw_text)

    normalized = {}
    for key, value in parsed.items():
        normalized[str(key)] = {
            "prob": max(0.0, min(100.0, to_float((value or {}).get("prob"), 50.0))),
            "conf": max(0, min(100, int(to_float((value or {}).get("conf"), 0.0)))),
        }
    return normalized


def write_csv(rows, csv_path):
    if not csv_path:
        return
    headers = [
        "ticker",
        "title",
        "signal_side",
        "yes_price",
        "no_price",
        "true_yes_prob",
        "true_no_prob",
        "confidence",
        "edge",
        "volume",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def parse_args():
    parser = argparse.ArgumentParser(description="Kalshi all-markets undervalued scanner with low-cost batching")
    parser.add_argument("--min-edge", type=float, default=MIN_EDGE_DEFAULT)
    parser.add_argument("--min-volume", type=float, default=MIN_VOLUME_DEFAULT)
    parser.add_argument("--scan-interval-seconds", type=int, default=SCAN_INTERVAL_SECONDS_DEFAULT)
    parser.add_argument("--max-markets", type=int, default=MAX_MARKETS_TO_ANALYZE_DEFAULT)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE_DEFAULT)
    parser.add_argument("--max-pages", type=int, default=min(OPEN_MARKETS_MAX_PAGES, 5))
    parser.add_argument("--csv", type=str, default="")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--demo-ai", action="store_true")
    return parser.parse_args()


def run_scan_cycle(args):
    markets = fetch_active_kalshi_markets(max_pages=max(1, args.max_pages), per_page_limit=500)

    high_volume_markets = sorted(
        [market for market in markets if get_volume(market) >= args.min_volume],
        key=get_volume,
        reverse=True,
    )[: max(1, args.max_markets)]

    print(
        f"\n{datetime.now().strftime('%H:%M:%S')} - Analyzing top {len(high_volume_markets)} highest-volume markets"
    )

    hits = []
    for i in range(0, len(high_volume_markets), max(1, args.batch_size)):
        batch = high_volume_markets[i:i + max(1, args.batch_size)]
        probs = get_ai_probabilities_batch(batch, demo_ai=args.demo_ai)

        for idx, market in enumerate(batch):
            ticker = market.get("ticker", "UNKNOWN")
            title = str(market.get("title", ""))[:70]
            yes_price = get_yes_price(market)
            no_price = get_no_price(market, yes_price)
            true_prob = to_float(probs.get(str(idx), {}).get("prob", 50.0), 50.0) / 100.0
            confidence = int(to_float(probs.get(str(idx), {}).get("conf", 0.0), 0.0))
            true_yes_prob = max(0.0, min(1.0, true_prob))
            true_no_prob = max(0.0, min(1.0, 1.0 - true_yes_prob))
            yes_edge = true_yes_prob - yes_price
            no_edge = true_no_prob - no_price

            print(
                f"   {ticker} | Yes @ ${yes_price:.2f} | No @ ${no_price:.2f} | "
                f"AI Yes: {true_yes_prob:.1%} | Yes edge: {yes_edge:+.1%} | No edge: {no_edge:+.1%}"
            )

            if yes_edge >= args.min_edge:
                print(f"     UNDERVALUED -> BUY YES! Edge +{yes_edge:.1%}")
                hits.append(
                    {
                        "ticker": ticker,
                        "title": title,
                        "signal_side": "YES",
                        "yes_price": round(yes_price, 4),
                        "no_price": round(no_price, 4),
                        "true_yes_prob": round(true_yes_prob, 4),
                        "true_no_prob": round(true_no_prob, 4),
                        "confidence": max(0, min(100, confidence)),
                        "edge": round(yes_edge, 4),
                        "volume": int(get_volume(market)),
                    }
                )

            if no_edge >= args.min_edge:
                print(f"     UNDERVALUED -> BUY NO! Edge +{no_edge:.1%}")
                hits.append(
                    {
                        "ticker": ticker,
                        "title": title,
                        "signal_side": "NO",
                        "yes_price": round(yes_price, 4),
                        "no_price": round(no_price, 4),
                        "true_yes_prob": round(true_yes_prob, 4),
                        "true_no_prob": round(true_no_prob, 4),
                        "confidence": max(0, min(100, confidence)),
                        "edge": round(no_edge, 4),
                        "volume": int(get_volume(market)),
                    }
                )

    hits.sort(key=lambda row: (-row["edge"], -row["volume"]))
    if args.csv:
        write_csv(hits, args.csv)
    return hits


def main():
    args = parse_args()
    print("KALSHI ALL-MARKETS Undervalued Scanner Started")
    print("   (Volume-only filter | Low-cost batching)")
    print("=" * 75)

    while True:
        hits = run_scan_cycle(args)
        print(f"Cycle done | undervalued hits: {len(hits)}")

        if args.once:
            break

        print(f"Sleeping {max(1, args.scan_interval_seconds) // 60} minutes...")
        time.sleep(max(1, args.scan_interval_seconds))


if __name__ == "__main__":
    main()
