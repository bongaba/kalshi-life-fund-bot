import json, sys
sys.path.insert(0, '.')
from position_monitor import signed_request

# Try market endpoint with correct path
ticker = 'KXWTIW-26APR10-T106.00'
try:
    m = signed_request('GET', f'/markets/{ticker}')
    print('=== Market Data ===')
    print(json.dumps(m, indent=2, default=str))
except Exception as e:
    print(f'Market fetch error: {e}')

print()

# Check event markets
try:
    event = signed_request('GET', '/markets', params={'event_ticker': 'KXWTIW-26APR10', 'limit': 20})
    markets = event.get('markets', [])
    print(f'=== Event KXWTIW-26APR10: {len(markets)} markets ===')
    for m in markets:
        t = m.get('ticker')
        status = m.get('status')
        result = m.get('result')
        yes_bid = m.get('yes_bid')
        yes_ask = m.get('yes_ask')
        title = m.get('title', '')[:60]
        close = m.get('close_time', '')
        print(f'  {t} | status={status} | result={result} | yes_bid={yes_bid} yes_ask={yes_ask} | close={close} | {title}')
except Exception as e:
    print(f'Event fetch error: {e}')
