import json

lines = [l for l in open(r'C:\Users\rbong\kalshi-life-fund-bot\logs\edge_signals\position_scores\pos_scores_20260419.jsonl') if '26APR1908-T75399' in l]
data = [json.loads(l) for l in lines]
print(f"Records: {len(data)}")
prices = [d['current_bid'] for d in data]
print(f"First: {data[0]['ts']} bid=${data[0]['current_bid']} pnl={data[0]['pnl_pct']}%")
print(f"Last:  {data[-1]['ts']} bid=${data[-1]['current_bid']} pnl={data[-1]['pnl_pct']}%")
print(f"Min bid: ${min(prices)}  Max bid: ${max(prices)}")
print(f"Worst pnl_pct: {min(d['pnl_pct'] for d in data)}%")
print(f"Entry price: ${data[0]['entry_price']}")
print(f"80% SL trigger at: ${data[0]['entry_price'] * 0.20:.3f}")
