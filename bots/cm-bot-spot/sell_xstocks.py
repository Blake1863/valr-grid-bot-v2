#!/usr/bin/env python3
"""
Sell excess xstock inventory on CMS2 for USDT/USDC.
Uses REST API POST /v1/orders/limit endpoint.
"""
import requests
import hmac
import hashlib
import time
import json

# Bot credentials
api_key = 'eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362'
api_secret = '9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db'
CMS2_ID = '1483815498551132160'
API_BASE = 'https://api.valr.com'

# xstocks to sell (base, quote, price_precision, min_qty to keep)
XSTOCKS = [
    ('JUP', 'USDT', 4, 1.0),
    ('EURC', 'USDC', 4, 1.0),
    ('TRUMP', 'USDT', 3, 0.5),
    ('SPYX', 'USDT', 2, 0.01),
    ('MSTRX', 'USDT', 2, 0.01),
    ('CRCLX', 'USDT', 2, 0.01),
    ('BITGOLD', 'USDT', 2, 0.01),
    ('NVDAX', 'USDT', 2, 0.01),
    ('HOODX', 'USDT', 2, 0.01),
    ('COINX', 'USDT', 2, 0.01),
    ('TSLAX', 'USDT', 2, 0.01),
    ('VALR10', 'USDT', 2, 0.01),
]

def sign(method, path, body):
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method.upper()}{path}{body}{CMS2_ID}"
    sig = hmac.new(api_secret.encode(), msg.encode(), hashlib.sha512).hexdigest()
    return ts, sig

def get_balance(currency):
    path = '/v1/account/balances'
    ts, sig = sign('GET', path, '')
    resp = requests.get(f'{API_BASE}{path}', headers={
        'X-VALR-API-KEY': api_key,
        'X-VALR-SIGNATURE': sig,
        'X-VALR-TIMESTAMP': ts,
        'X-VALR-SUB-ACCOUNT-ID': CMS2_ID,
    })
    for b in resp.json():
        if b['currency'] == currency:
            return float(b.get('available', 0))
    return 0

def get_price(pair):
    resp = requests.get(f'{API_BASE}/v1/public/{pair}/orderbook')
    data = resp.json()
    bids = data.get('Bids', data.get('bids', []))
    if bids:
        return float(bids[0]['price'])
    return None

def place_limit_sell(base, quote, qty, price_prec, min_qty):
    """Place IOC limit sell order."""
    pair = f"{base}{quote}"
    price = get_price(pair)
    if not price:
        print(f"  ❌ {pair}: No price found")
        return
    
    # Keep min_qty reserve, sell the rest
    sell_qty = qty - min_qty
    if sell_qty <= 0:
        print(f"  ⏭️  {pair}: Only {qty:.4f} (keeping as reserve)")
        return
    
    # Aggressive pricing: 2% below best bid to ensure fill
    sell_price = round(price * 0.98, price_prec)
    
    # Use /v1/orders/limit endpoint
    path = '/v1/orders/limit'
    body = json.dumps({
        'pair': pair,
        'side': 'SELL',
        'quantity': f"{sell_qty:.8f}",
        'price': f"{sell_price}",
        'timeInForce': 'IOC',
        'customerOrderId': f"liq-{base}-{int(time.time())}"
    })
    
    ts, sig = sign('POST', path, body)
    resp = requests.post(f'{API_BASE}{path}', headers={
        'X-VALR-API-KEY': api_key,
        'X-VALR-SIGNATURE': sig,
        'X-VALR-TIMESTAMP': ts,
        'X-VALR-SUB-ACCOUNT-ID': CMS2_ID,
        'Content-Type': 'application/json',
    }, data=body)
    
    if resp.status_code == 202:
        result = resp.json()
        print(f"  ✅ {pair}: Selling {sell_qty:.4f} @ {sell_price} → {result.get('id', 'pending')}")
    else:
        print(f"  ❌ {pair}: {resp.status_code} - {resp.text[:100]}")

def main():
    print("=== CMS2 Xstock Liquidation ===\n")
    
    for base, quote, price_prec, min_qty in XSTOCKS:
        balance = get_balance(base)
        if balance > min_qty:
            place_limit_sell(base, quote, balance, price_prec, min_qty)
        elif balance > 0:
            print(f"  ⏭️  {base}{quote}: {balance:.4f} (keeping as reserve)")
    
    print("\n=== Done ===")

if __name__ == '__main__':
    main()
