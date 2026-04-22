"""Stop-loss backtest analyzer.

Reads the structured JSONL stop-loss log and trades.db, then checks actual market
settlement outcomes via the Kalshi API to answer:

  "Did each stop-loss exit save us money, or would holding to settlement have been better?"

For each SL exit:
  - exit_pnl: what we actually realized by selling early
  - hold_pnl: what we'd have gotten holding to settlement ($1 win / $0 lose)
  - sl_value: exit_pnl - hold_pnl  (positive = SL saved us money)

Usage:
  python analyze_stop_loss.py              # analyze all SL exits
  python analyze_stop_loss.py --ticker KXBTCD-26APR0921-T71799.99   # single ticker
  python analyze_stop_loss.py --csv        # export to CSV
"""

import json
import glob
import sqlite3
import os
import sys
import time
import base64
import argparse
from datetime import datetime, timezone
from pathlib import Path

# --- Kalshi API auth (reuse from config) ---
try:
    from config import KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, MODE
except ImportError:
    print("ERROR: config.py not found. Run from the project root.")
    sys.exit(1)

import requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding

host = "https://demo-api.kalshi.co" if MODE == "demo" else "https://api.elections.kalshi.com"
api_prefix = "/trade-api/v2"

private_key = None
try:
    with open(KALSHI_PRIVATE_KEY_PATH, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
except Exception as e:
    print(f"ERROR: Failed to load private key: {e}")
    sys.exit(1)


def signed_request(method, path, params=None):
    timestamp = str(int(time.time() * 1000))
    full_path = api_prefix + path
    if params:
        full_path += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    sign_path = (api_prefix + path).split('?')[0]
    message = f"{timestamp}{method.upper()}{sign_path}".encode('utf-8')
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256()
    )
    sig_b64 = base64.b64encode(signature).decode('utf-8')
    headers = {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }
    url = f"{host}{full_path}"
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


# --- Market settlement cache ---
_market_cache = {}

def get_market_settlement(ticker):
    """Fetch market result from Kalshi API. Returns dict with status, result, etc."""
    if ticker in _market_cache:
        return _market_cache[ticker]
    try:
        data = signed_request("GET", f"/markets/{ticker}")
        market = data.get("market", {})
        info = {
            "status": (market.get("status") or "").lower(),
            "result": (market.get("result") or "").lower(),       # "yes", "no", or ""
            "close_time": market.get("close_time"),
            "settlement_value": market.get("settlement_value"),     # sometimes present
        }
        _market_cache[ticker] = info
        return info
    except Exception as e:
        print(f"  WARNING: Could not fetch market {ticker}: {e}")
        return {"status": "unknown", "result": "", "close_time": None, "settlement_value": None}


# --- Load JSONL stop-loss log ---
def load_sl_events(log_dir="logs/stop_loss"):
    """Load all exit_executed events from JSONL log files."""
    events = []
    pattern = os.path.join(log_dir, "*.jsonl")
    files = sorted(glob.glob(pattern))
    if not files:
        return events
    for filepath in files:
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    events.append(record)
                except json.JSONDecodeError:
                    continue
    return events


# --- Load SL-closed trades from DB ---
def load_sl_trades_from_db():
    """Load trades closed by stop_loss or momentum_reversal from trades.db."""
    if not os.path.exists("trades.db"):
        return []
    conn = sqlite3.connect("trades.db", timeout=5)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT market_ticker, direction, price, size, pnl, fees, reason, status, resolved_timestamp
        FROM trades
        WHERE (reason LIKE '%closed by stop_loss%' OR reason LIKE '%closed by momentum_reversal%')
          AND status = 'CLOSED'
        ORDER BY resolved_timestamp ASC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def compute_hold_pnl(direction, entry_price, contracts, entry_fees, settlement_result):
    """Compute PnL if we had held to settlement instead of exiting.

    Binary contract: settles at $1.00 if our side wins, $0.00 if our side loses.
    """
    if not settlement_result or settlement_result not in ("yes", "no"):
        return None  # market not settled yet

    our_side_won = (
        (direction == "YES" and settlement_result == "yes") or
        (direction == "NO" and settlement_result == "no")
    )
    settlement_price = 1.00 if our_side_won else 0.00
    hold_pnl = contracts * (settlement_price - entry_price) - entry_fees
    return hold_pnl


def analyze():
    parser = argparse.ArgumentParser(description="Stop-loss backtest analyzer")
    parser.add_argument("--ticker", help="Analyze a single ticker only")
    parser.add_argument("--csv", action="store_true", help="Export results to CSV")
    parser.add_argument("--all-events", action="store_true", help="Show all SL events, not just exits")
    args = parser.parse_args()

    print("=" * 80)
    print("STOP-LOSS BACKTEST ANALYSIS")
    print("=" * 80)
    print()

    # --- Load data from both sources ---
    sl_events = load_sl_events()
    db_trades = load_sl_trades_from_db()

    exit_events = [e for e in sl_events if e.get("event") == "exit_executed"]
    all_event_types = {}
    for e in sl_events:
        t = e.get("event", "unknown")
        all_event_types[t] = all_event_types.get(t, 0) + 1

    print(f"JSONL log events loaded: {len(sl_events)}")
    for etype, count in sorted(all_event_types.items()):
        print(f"  {etype}: {count}")
    print(f"DB trades closed by SL: {len(db_trades)}")
    print()

    if args.all_events:
        print("-" * 80)
        print("ALL STOP-LOSS EVENTS (chronological)")
        print("-" * 80)
        for e in sl_events:
            ticker_filter = args.ticker
            if ticker_filter and e.get("ticker") != ticker_filter:
                continue
            ts = e.get("ts", "?")[:19]
            event = e.get("event", "?")
            ticker = e.get("ticker", "?")
            pnl = e.get("pnl") or e.get("realized_pnl")
            pnl_str = f"${pnl:.2f}" if pnl is not None else "n/a"
            extra = ""
            if "breach_elapsed" in e:
                extra += f" wait={e['breach_elapsed']:.1f}/{e.get('effective_wait', '?')}s"
            if e.get("has_pressure"):
                extra += f" pressure={e.get('pressure_side', '?').upper()}"
            if "bid_size" in e:
                extra += f" bid_size={e['bid_size']}"
            print(f"  {ts} | {event:30s} | {ticker:40s} | pnl={pnl_str}{extra}")
        print()

    # --- Merge: prefer JSONL exit events, fall back to DB ---
    exits = {}  # ticker -> analysis record

    # From JSONL log (richest data)
    for e in exit_events:
        tk = e.get("ticker")
        if args.ticker and tk != args.ticker:
            continue
        exits[tk] = {
            "ticker": tk,
            "direction": e.get("direction"),
            "contracts": e.get("contracts"),
            "entry_price": e.get("entry_price"),
            "exit_price": e.get("exit_price"),
            "entry_fees": e.get("entry_fees", 0),
            "exit_fees": e.get("exit_fees", 0),
            "realized_pnl": e.get("realized_pnl"),
            "trigger": e.get("trigger"),
            "ts": e.get("ts"),
            "source": "jsonl",
        }

    # From DB (fallback for any exits not in JSONL)
    for row in db_trades:
        tk = row["market_ticker"]
        if args.ticker and tk != args.ticker:
            continue
        if tk in exits:
            continue  # already have JSONL data
        contracts = int(round(row["size"] * 100))
        trigger = "stop_loss" if "stop_loss" in row["reason"] else "momentum_reversal"
        exits[tk] = {
            "ticker": tk,
            "direction": row["direction"],
            "contracts": contracts,
            "entry_price": row["price"],
            "exit_price": None,  # DB pnl = (exit - entry) * size, so exit = entry + pnl/size
            "entry_fees": row.get("fees", 0) or 0,
            "exit_fees": 0,
            "realized_pnl": row["pnl"],
            "trigger": trigger,
            "ts": row["resolved_timestamp"],
            "source": "db",
        }
        # Derive exit price from PnL
        if row["size"] > 0 and row["pnl"] is not None:
            exits[tk]["exit_price"] = round(row["price"] + row["pnl"] / row["size"], 4)

    if not exits:
        print("No stop-loss exits found yet. The system will log events to logs/stop_loss/*.jsonl")
        print("as positions trigger stop-loss rules. Re-run this script after some SL exits occur.")
        print()
        print("Current open position(s) in DB:")
        if os.path.exists("trades.db"):
            conn = sqlite3.connect("trades.db", timeout=5)
            for row in conn.execute("SELECT market_ticker, direction, price, size FROM trades WHERE status='OPEN'").fetchall():
                print(f"  {row[0]} | {row[1]} | entry=${row[2]:.4f} | size={row[3]}")
            conn.close()
        return

    # --- Analyze each exit against settlement ---
    print("-" * 80)
    print("EXIT-BY-EXIT ANALYSIS")
    print("-" * 80)

    results = []
    total_exit_pnl = 0.0
    total_hold_pnl = 0.0
    settled_count = 0
    unsettled_count = 0
    sl_saved = 0
    sl_cost = 0

    for tk, ex in sorted(exits.items(), key=lambda x: x[1].get("ts") or ""):
        print(f"\n  {ex['ticker']}")
        print(f"    Trigger:    {ex['trigger']}")
        print(f"    Direction:  {ex['direction']}")
        print(f"    Contracts:  {ex['contracts']}")
        print(f"    Entry:      ${ex['entry_price']:.4f}")
        print(f"    Exit:       ${ex['exit_price']:.4f}" if ex['exit_price'] else "    Exit:       unknown")
        print(f"    Exit PnL:   ${ex['realized_pnl']:.4f}" if ex['realized_pnl'] is not None else "    Exit PnL:   unknown")
        print(f"    Exit time:  {ex['ts']}")

        # Fetch settlement
        market_info = get_market_settlement(tk)
        print(f"    Market:     status={market_info['status']}, result={market_info['result'] or 'pending'}")

        hold_pnl = compute_hold_pnl(
            ex["direction"],
            ex["entry_price"],
            ex["contracts"],
            ex["entry_fees"],
            market_info["result"],
        )

        result_row = {**ex, "market_status": market_info["status"], "market_result": market_info["result"]}

        if hold_pnl is not None:
            settled_count += 1
            sl_value = (ex["realized_pnl"] or 0) - hold_pnl
            result_row["hold_pnl"] = round(hold_pnl, 4)
            result_row["sl_value"] = round(sl_value, 4)

            total_exit_pnl += ex["realized_pnl"] or 0
            total_hold_pnl += hold_pnl

            verdict = "SAVED" if sl_value > 0 else "COST" if sl_value < 0 else "NEUTRAL"
            if sl_value > 0:
                sl_saved += 1
            elif sl_value < 0:
                sl_cost += 1

            our_side_won = (
                (ex["direction"] == "YES" and market_info["result"] == "yes") or
                (ex["direction"] == "NO" and market_info["result"] == "no")
            )

            print(f"    Hold PnL:   ${hold_pnl:.4f} (would have {'WON' if our_side_won else 'LOST'})")
            print(f"    SL Value:   ${sl_value:.4f}  ← {verdict}")
        else:
            unsettled_count += 1
            result_row["hold_pnl"] = None
            result_row["sl_value"] = None
            print(f"    Hold PnL:   pending (market not settled)")

        results.append(result_row)

    # --- Summary ---
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"  Total SL exits:        {len(results)}")
    print(f"  Settled markets:       {settled_count}")
    print(f"  Pending settlement:    {unsettled_count}")
    if settled_count > 0:
        print(f"  Total exit PnL:        ${total_exit_pnl:.2f}  (what we got)")
        print(f"  Total hold PnL:        ${total_hold_pnl:.2f}  (what we'd have gotten)")
        net = total_exit_pnl - total_hold_pnl
        print(f"  Net SL value:          ${net:.2f}  ({'SL SAVED money' if net > 0 else 'SL COST money' if net < 0 else 'NEUTRAL'})")
        print(f"  SL was correct:        {sl_saved}/{settled_count} ({sl_saved/settled_count*100:.0f}%)")
        print(f"  SL was noise:          {sl_cost}/{settled_count} ({sl_cost/settled_count*100:.0f}%)")

    # --- Event frequency stats ---
    if all_event_types:
        print()
        print("  Event frequency (how often each SL code path fires):")
        for etype, count in sorted(all_event_types.items(), key=lambda x: -x[1]):
            print(f"    {etype:35s} {count:5d}x")
        breach_wait = all_event_types.get("breach_waiting", 0)
        breach_exit = all_event_types.get("breach_sustained_exit", 0) + all_event_types.get("severe_breach_exit", 0)
        if breach_wait + breach_exit > 0:
            print(f"  Breach→exit conversion rate:  {breach_exit}/{breach_wait + breach_exit} "
                  f"({breach_exit/(breach_wait+breach_exit)*100:.0f}%)")

    # --- CSV export ---
    if args.csv and results:
        csv_path = f"data/sl_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        os.makedirs("data", exist_ok=True)
        import csv
        fields = ["ticker", "direction", "trigger", "contracts", "entry_price", "exit_price",
                  "realized_pnl", "hold_pnl", "sl_value", "market_status", "market_result",
                  "entry_fees", "exit_fees", "ts", "source"]
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)
        print(f"\n  CSV exported: {csv_path}")

    print()


if __name__ == "__main__":
    analyze()
