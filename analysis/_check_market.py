import json, sys
sys.path.insert(0, '.')
from position_monitor import signed_request

# Check current market
m = signed_request('GET', '/markets/KXWTIW-26APR10-T106.00')
print('Market:', m.get('title'))
print('Status:', m.get('status'))
print('Result:', m.get('result'))
print('Yes bid:', m.get('yes_bid'))
print('Yes ask:', m.get('yes_ask'))
print('No bid:', m.get('no_bid'))
print('No ask:', m.get('no_ask'))
print('Close time:', m.get('close_time'))
print()

# Check portfolio positions
pos = signed_request('GET', '/portfolio/positions', params={'ticker': 'KXWTIW-26APR10-T106.00'})
print('Position:', json.dumps(pos, indent=2))
