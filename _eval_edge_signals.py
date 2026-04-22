"""
Evaluate edge scanner signal_log performance.
For each signal, check whether the market settled in the predicted direction.
Uses Kalshi API to look up settlement results.
"""
import json, os, sys, time, base64
from datetime import datetime, timezone
from collections import defaultdict
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import MODE, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

host = "https://demo-api.kalshi.co" if MODE == "demo" else "https://api.elections.kalshi.com"
api_prefix = "/trade-api/v2"

with open(KALSHI_PRIVATE_KEY_PATH, "rb") as f:
    private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def signed_request(method, path, params=None):
    timestamp = str(int(time.time() * 1000))
    full_path = api_prefix + path
    sign_path = full_path.split('?')[0]
    message = f"{timestamp}{method}{sign_path}".encode('utf-8')
    sig = private_key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    headers = {"KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID, "KALSHI-ACCESS-TIMESTAMP": timestamp, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}
    url = host + full_path
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    return resp

def fetch_market(ticker):
    resp = signed_request("GET", f"/markets/{ticker}")
    if resp.status_code == 200:
        return resp.json().get("market", {})
    return None

# Load all signals
signals = []
for fname in sorted(os.listdir("logs/edge_signals")):
    if not fname.endswith(".jsonl"):
        continue
    with open(os.path.join("logs/edge_signals", fname), "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                signals.append(rec)
            except:
                continue

print(f"Total signals loaded: {len(signals)}", flush=True)

# Deduplicate: keep last signal per ticker (most recent reading before close)
by_ticker = {}
for s in signals:
    tk = s["ticker"]
    if tk not in by_ticker or s["ts"] > by_ticker[tk]["ts"]:
        by_ticker[tk] = s

print(f"Unique tickers: {len(by_ticker)}", flush=True)

# Separate filtered vs unfiltered (would-trade) signals
filtered_tickers = {tk: s for tk, s in by_ticker.items() if s.get("filtered")}
unfiltered_tickers = {tk: s for tk, s in by_ticker.items() if not s.get("filtered")}
print(f"Would-trade (unfiltered): {len(unfiltered_tickers)}", flush=True)
print(f"Filtered out: {len(filtered_tickers)}", flush=True)
print(f"Fetching settlement data for {len(by_ticker)} markets...", flush=True)

# Look up settlement for each unique ticker
cache = {}
fetched = 0
total_to_fetch = len(by_ticker)
def get_settlement(ticker):
    global fetched
    if ticker in cache:
        return cache[ticker]
    try:
        mkt = fetch_market(ticker)
    except Exception as e:
        print(f"  ERROR fetching {ticker}: {e}", flush=True)
        return None
    fetched += 1
    if fetched % 20 == 0:
        print(f"  Fetched {fetched}/{total_to_fetch}...", flush=True)
    time.sleep(0.12)  # rate limit
    if mkt:
        cache[ticker] = mkt
    return mkt

# Evaluate
results = {"unfiltered": [], "filtered": []}
for label, group in [("unfiltered", unfiltered_tickers), ("filtered", filtered_tickers)]:
    for tk, sig in sorted(group.items(), key=lambda x: x[1]["ts"]):
        mkt = get_settlement(tk)
        if not mkt:
            continue
        status = mkt.get("status", "")
        result = mkt.get("result", "")
        if status not in ("finalized", "settled"):
            # Not yet settled
            results[label].append({**sig, "outcome": "pending", "result": result})
            continue
        
        predicted_dir = sig["direction"]
        won = (predicted_dir == "YES" and result == "yes") or (predicted_dir == "NO" and result == "no")
        
        # Calculate hypothetical PnL
        if predicted_dir == "YES":
            entry_price = sig.get("yes_price", 0)
        else:
            entry_price = sig.get("no_price", 0)
        
        if won:
            pnl_per_contract = 1.0 - entry_price - 0.02  # ~2c fee
        else:
            pnl_per_contract = -entry_price - 0.02
        
        results[label].append({
            **sig, 
            "outcome": "won" if won else "lost",
            "result": result,
            "entry_price": entry_price,
            "pnl_per_contract": round(pnl_per_contract, 4),
        })

# Print results
import io
out = io.StringIO()
def p(s=""):
    print(s, flush=True)
    out.write(s + "\n")

for label in ["unfiltered", "filtered"]:
    group = results[label]
    settled = [r for r in group if r["outcome"] in ("won", "lost")]
    pending = [r for r in group if r["outcome"] == "pending"]
    wins = [r for r in settled if r["outcome"] == "won"]
    losses = [r for r in settled if r["outcome"] == "lost"]
    
    p(f"\n{'='*80}")
    p(f"  {label.upper()} SIGNALS ({'would trade' if label == 'unfiltered' else 'rejected by filters'})")
    p(f"{'='*80}")
    p(f"Total: {len(group)} | Settled: {len(settled)} | Pending: {len(pending)}")
    if settled:
        p(f"Wins: {len(wins)} | Losses: {len(losses)} | Win Rate: {len(wins)/len(settled)*100:.1f}%")
        total_pnl = sum(r["pnl_per_contract"] for r in settled)
        avg_pnl = total_pnl / len(settled)
        avg_win = sum(r["pnl_per_contract"] for r in wins) / len(wins) if wins else 0
        avg_loss = sum(r["pnl_per_contract"] for r in losses) / len(losses) if losses else 0
        p(f"Avg PnL/contract: ${avg_pnl:.4f} | Avg Win: ${avg_win:.4f} | Avg Loss: ${avg_loss:.4f}")
        p(f"Total hypothetical PnL (1 contract each): ${total_pnl:.4f}")
    
    # Score breakdown
    if settled:
        p(f"\n  By Score Bucket:")
        buckets = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0})
        for r in settled:
            sc = r["score"]
            if sc >= 80:
                bk = "80+"
            elif sc >= 70:
                bk = "70-79"
            elif sc >= 60:
                bk = "60-69"
            else:
                bk = "<60"
            buckets[bk]["w" if r["outcome"] == "won" else "l"] += 1
            buckets[bk]["pnl"] += r["pnl_per_contract"]
        
        for bk in ["80+", "70-79", "60-69", "<60"]:
            if bk in buckets:
                b = buckets[bk]
                total = b["w"] + b["l"]
                wr = b["w"] / total * 100 if total else 0
                p(f"    {bk:>6}: {b['w']}W / {b['l']}L  ({wr:.0f}%)  PnL: ${b['pnl']:.4f}")
    
    # Direction breakdown
    if settled:
        p(f"\n  By Direction:")
        for d in ["YES", "NO"]:
            ds = [r for r in settled if r["direction"] == d]
            dw = [r for r in ds if r["outcome"] == "won"]
            if ds:
                p(f"    {d}: {len(dw)}W / {len(ds)-len(dw)}L  ({len(dw)/len(ds)*100:.0f}%)  Avg entry: ${sum(r['entry_price'] for r in ds)/len(ds):.3f}")
    
    # Entry price breakdown
    if settled:
        p(f"\n  By Entry Price:")
        price_buckets = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0})
        for r in settled:
            ep = r["entry_price"]
            if ep < 0.30:
                pb = "<0.30"
            elif ep <= 0.50:
                pb = "0.30-0.50"
            elif ep <= 0.70:
                pb = "0.51-0.70"
            elif ep <= 0.85:
                pb = "0.71-0.85"
            else:
                pb = ">0.85"
            price_buckets[pb]["w" if r["outcome"] == "won" else "l"] += 1
            price_buckets[pb]["pnl"] += r["pnl_per_contract"]
        for pb in ["<0.30", "0.30-0.50", "0.51-0.70", "0.71-0.85", ">0.85"]:
            if pb in price_buckets:
                b = price_buckets[pb]
                total = b["w"] + b["l"]
                wr = b["w"] / total * 100 if total else 0
                p(f"    {pb:>10}: {b['w']}W / {b['l']}L  ({wr:.0f}%)  PnL: ${b['pnl']:.4f}")

    # Detail each settled signal
    p(f"\n  Detailed results:")
    for r in sorted(settled, key=lambda x: x["ts"]):
        marker = "W" if r["outcome"] == "won" else "L"
        filt = " [FILTERED]" if r.get("filtered") else ""
        reasons = f" ({', '.join(r.get('filter_reasons', []))})" if r.get("filter_reasons") else ""
        p(f"    [{marker}] {r['ticker']:40s} {r['direction']:3s} @ ${r['entry_price']:.3f}  score={r['score']:5.1f}  pnl=${r['pnl_per_contract']:+.4f}{filt}{reasons}")

p(f"\n{'='*80}")
p("DONE")

with open("_edge_eval_results.txt", "w") as f:
    f.write(out.getvalue())
print(f"\nResults written to _edge_eval_results.txt")
