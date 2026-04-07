#!/usr/bin/env python3
"""
Close Bybit position and cancel all grid orders.
"""

import os
import hmac
import hashlib
import time
import requests
import subprocess

BASE_URL = 'https://api.bybit.com'
RECV_WINDOW = '5000'
SYMBOL = 'SOLUSDT'

def get_secret(name):
    return subprocess.run(f"python3 /home/admin/.openclaw/secrets/secrets.py get {name}", 
                         shell=True, capture_output=True, text=True).stdout.strip()

def get_api_key():
    return get_secret('bybit_api_key')

def get_api_secret():
    return get_secret('bybit_api_secret')

def bybit_get(endpoint, params=''):
    api_key = get_api_key()
    api_secret = get_api_secret()
    timestamp = str(int(time.time() * 1000))
    param_str = timestamp + api_key + RECV_WINDOW + params
    signature = hmac.new(api_secret.encode(), param_str.encode(), hashlib.sha256).hexdigest()
    
    headers = {
        'X-BAPI-API-KEY': api_key,
        'X-BAPI-TIMESTAMP': timestamp,
        'X-BAPI-SIGN': signature,
        'X-BAPI-RECV-WINDOW': RECV_WINDOW,
    }
    
    resp = requests.get(f'{BASE_URL}{endpoint}?{params}', headers=headers, timeout=10)
    return resp.json()

def bybit_post(endpoint, body):
    api_key = get_api_key()
    api_secret = get_api_secret()
    timestamp = str(int(time.time() * 1000))
    import json
    body_str = json.dumps(body)  # Default separators with spaces
    param_str = timestamp + api_key + RECV_WINDOW + body_str
    signature = hmac.new(api_secret.encode(), param_str.encode(), hashlib.sha256).hexdigest()
    
    headers = {
        'X-BAPI-API-KEY': api_key,
        'X-BAPI-TIMESTAMP': timestamp,
        'X-BAPI-SIGN': signature,
        'X-BAPI-RECV-WINDOW': RECV_WINDOW,
        'Content-Type': 'application/json',
    }
    
    resp = requests.post(f'{BASE_URL}{endpoint}', headers=headers, json=body, timeout=10)
    return resp.json()

def get_position():
    params = f'category=linear&symbol={SYMBOL}'
    data = bybit_get('/v5/position/list', params)
    if data.get('retCode') == 0 and data['result']['list']:
        return data['result']['list'][0]
    return None

def get_open_orders():
    params = f'category=linear&symbol={SYMBOL}'
    data = bybit_get('/v5/order/realtime', params)
    if data.get('retCode') == 0:
        return data['result']['list']
    return []

def cancel_all_orders():
    body = {'category': 'linear', 'symbol': SYMBOL}
    data = bybit_post('/v5/order/cancel-all', body)
    return data.get('retCode') == 0, data.get('retMsg', '')

def close_position(side, qty):
    """Close position with market order."""
    body = {
        'category': 'linear',
        'symbol': SYMBOL,
        'side': 'Sell' if side == 'Buy' else 'Buy',  # Opposite to close
        'orderType': 'Market',
        'qty': str(qty),
        'timeInForce': 'IOC',
        'reduceOnly': True,
        'positionIdx': 0,
    }
    return bybit_post('/v5/order/create', body)

def main():
    print("=" * 60)
    print("🔄 Closing Bybit Position & Canceling Grid Orders")
    print("=" * 60)
    
    # Check position
    print("\n📊 Checking position...")
    pos = get_position()
    
    if not pos or float(pos.get('size', 0)) == 0:
        print("  ℹ️  No open position")
    else:
        side = pos.get('side')
        size = float(pos.get('size', 0))
        entry = float(pos.get('avgPrice', 0))
        pnl = float(pos.get('unrealisedPnl', 0))
        
        print(f"  Position: {side} {size} SOL @ ${entry:.2f}")
        print(f"  Unrealized PnL: ${pnl:.2f}")
    
    # Cancel all orders
    print("\n❌ Canceling all open orders...")
    orders = get_open_orders()
    print(f"  Found {len(orders)} open orders")
    
    if len(orders) > 0:
        success, msg = cancel_all_orders()
        if success:
            print(f"  ✅ All orders canceled")
        else:
            print(f"  ⚠️  Cancel failed: {msg}")
    else:
        print("  ℹ️  No orders to cancel")
    
    # Close position
    if pos and float(pos.get('size', 0)) > 0:
        print(f"\n🔨 Closing position ({side} {size} SOL)...")
        result = close_position(side, size)
        
        if result.get('retCode') == 0:
            order_id = result['result']['orderId']
            print(f"  ✅ Close order placed: {order_id[:16]}...")
            
            # Wait for fill
            print("  ⏳ Waiting for fill...")
            time.sleep(2)
            
            # Check updated position
            pos = get_position()
            if not pos or float(pos.get('size', 0)) == 0:
                print("  ✅ Position closed successfully")
            else:
                print(f"  ⚠️  Position still open: {pos.get('size')} SOL")
        else:
            print(f"  ❌ Close failed: {result.get('retMsg')}")
    
    # Final balance check
    print("\n💰 Final balance check...")
    data = bybit_get('/v5/account/wallet-balance', 'accountType=UNIFIED')
    if data.get('retCode') == 0:
        account = data['result']['list'][0]
        equity = float(account.get('totalEquity', 0))
        available = 0
        for coin in account.get('coin', []):
            if coin['coin'] == 'USDT':
                avail_str = coin.get('availableToWithdraw', '0')
                available = float(avail_str) if avail_str else 0
                break
        print(f"  Equity: ${equity:.2f}")
        print(f"  Available: ${available:.2f}")
    
    print("\n" + "=" * 60)
    print("✅ Done!")
    print("=" * 60)

if __name__ == "__main__":
    main()
