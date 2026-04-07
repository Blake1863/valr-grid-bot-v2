#!/usr/bin/env python3
"""
Bybit position monitor - check open positions and update trade log.
Runs silently, updates trade-log.md with current PnL and status.
"""
import os, json, hmac, hashlib, time, requests
from datetime import datetime

SECRETS_CMD = "python3 /home/admin/.openclaw/secrets/secrets.py get"
BASE_URL = 'https://api.bybit.com'
RECV_WINDOW = '5000'
LOG_FILE = '/home/admin/.openclaw/workspace/bots/bybit-trading/trade-log.md'

def get_secret(name):
    result = os.popen(f"{SECRETS_CMD} {name}").read().strip()
    return result

def bybit_get(endpoint, params=''):
    api_key = get_secret('bybit_api_key')
    api_secret = get_secret('bybit_api_secret')
    timestamp = str(int(time.time() * 1000))
    param_str = timestamp + api_key + RECV_WINDOW + params
    signature = hmac.new(api_secret.encode(), param_str.encode(), hashlib.sha256).hexdigest()
    
    headers = {
        'X-BAPI-API-KEY': api_key,
        'X-BAPI-TIMESTAMP': timestamp,
        'X-BAPI-SIGN': signature,
        'X-BAPI-RECV-WINDOW': RECV_WINDOW,
        'User-Agent': 'bybit-skill/1.1.1',
        'X-Referer': 'bybit-skill'
    }
    
    resp = requests.get(f'{BASE_URL}{endpoint}?{params}', headers=headers)
    return resp.json()

def get_positions():
    data = bybit_get('/v5/position/list', 'category=linear&settleCoin=USDT')
    if data.get('retCode') != 0:
        print(f"Error: {data}")
        return []
    return data['result']['list']

def get_balance():
    data = bybit_get('/v5/account/wallet-balance', 'accountType=UNIFIED')
    if data.get('retCode') != 0:
        return None
    return data['result']['list'][0]

def main():
    positions = get_positions()
    balance = get_balance()
    
    if not positions:
        print("No open positions")
        return
    
    print(f"=== Position Check @ {datetime.now().isoformat()} ===")
    
    for pos in positions:
        symbol = pos['symbol']
        side = pos['side']
        size = float(pos['size'])
        entry = float(pos['avgPrice'])
        mark = float(pos['markPrice'])
        unrealized = float(pos['unrealisedPnl'])
        tp = pos['takeProfit']
        sl = pos['stopLoss']
        leverage = pos['leverage']
        
        pnl_pct = (unrealized / (size * entry)) * 100 if size * entry > 0 else 0
        
        print(f"{symbol} {side} {size} @ {entry}")
        print(f"  Mark: {mark} | PnL: ${unrealized:.2f} ({pnl_pct:.2f}%)")
        print(f"  TP: {tp or 'None'} | SL: {sl or 'None'} | Lev: {leverage}x")
    
    if balance:
        equity = float(balance['totalEquity'])
        available = float(balance['totalAvailableBalance'])
        print(f"\nEquity: ${equity:.2f} | Available: ${available:.2f}")
    
    # Update trade log
    update_log(positions, balance)

def update_log(positions, balance):
    if not positions:
        return
    
    pos = positions[0]  # Assume single position for now
    symbol = pos['symbol']
    side = pos['side']
    size = float(pos['size'])
    entry = float(pos['avgPrice'])
    mark = float(pos['markPrice'])
    unrealized = float(pos['unrealisedPnl'])
    tp = pos['takeProfit']
    sl = pos['stopLoss']
    leverage = pos['leverage']
    
    equity = float(balance['totalEquity']) if balance else 0
    available = float(balance['totalAvailableBalance']) if balance else 0
    
    tp_display = f"${tp} ({((float(tp) - entry) / entry * 100):.1f}%) ✅" if tp else "None"
    sl_display = f"${sl} ({((float(sl) - entry) / entry * 100):.1f}%) ✅" if sl else "None"
    
    log_content = f"""# Bybit Trading Log

**Start Date:** 2026-03-25
**Starting Capital:** $122.48 USDT
**Goal:** Prove profitability in 7 days → scale to $500

---

## Trade #1 - {symbol} {side}
| Field | Value |
|-------|-------|
| Opened | 2026-03-25 14:42 UTC |
| Direction | {side} |
| Size | {size} |
| Entry | ${entry:,.1f} |
| Leverage | {leverage}x |
| Position Value | ${size * entry:.2f} |
| Stop Loss | {sl_display if sl else 'None'} |
| Take Profit | {tp_display if tp else 'None'} |
| Current Price | ${mark:,.1f} |
| Unrealized PnL | ${unrealized:.2f} ({(unrealized / (size * entry) * 100):.2f}%) |
| Status | {'OPEN (protected)' if tp and sl else 'OPEN (partially protected ⚠️)'} |

---

## Performance Summary
| Metric | Value |
|--------|-------|
| Total Trades | 1 |
| Open PnL | ${unrealized:.2f} |
| Realized PnL | ${float(pos['cumRealisedPnl']):.2f} |
| Total Equity | ${equity:.2f} |
| Available Balance | ${available:.2f} |
| Win Rate | - |

---

## Notes
- Position protected with TP/SL via `/v5/position/trading-stop`
- Auto-updated: {datetime.now().isoformat()}
"""
    
    with open(LOG_FILE, 'w') as f:
        f.write(log_content)
    
    print(f"\n✅ Trade log updated: {LOG_FILE}")

if __name__ == '__main__':
    main()
