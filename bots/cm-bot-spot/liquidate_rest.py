#!/usr/bin/env python3
"""
Liquidate xstock inventory on CMS2 via REST API.
Places IOC sell orders at aggressive prices (effectively market sells).
"""
import json
import time
import hmac
import hashlib
import requests
from datetime import datetime

# Load credentials from bot's .env file
api_key = 'eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362'
api_secret = '9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db'

# Configuration
CMS2_ID = '1483815498551132160'
API_BASE = 'https://api.valr.com'

# xstock pairs to liquidate (base, quote, price_precision)
XSTOCK_PAIRS = [
    ('SPYX', 'USDT', 2), ('MSTRX', 'USDT', 2), ('JUP', 'USDT', 4),
    ('CRCLX', 'USDT', 2), ('BITGOLD', 'USDT', 2), ('NVDAX', 'USDT', 2),
    ('HOODX', 'USDT', 2), ('COINX', 'USDT', 2), ('TSLAX', 'USDT', 2),
    ('VALR10', 'USDT', 2), ('TRUMP', 'USDT', 3), ('EURC', 'USDC', 4)
]

def sign_request(method, path, body, subaccount_id):
    """Generate VALR HMAC-SHA512 signature."""
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method.upper()}{path}{body}{subaccount_id}"
    signature = hmac.new(api_secret.encode(), message.encode(), hashlib.sha512).hexdigest()
    return timestamp, signature

def get_balances():
    """Get CMS2 balances via REST."""
    path = '/v1/account/balances'
    ts, sig = sign_request('GET', path, '', CMS2_ID)
    
    headers = {
        'X-VALR-API-KEY': api_key,
        'X-VALR-SIGNATURE': sig,
        'X-VALR-TIMESTAMP': ts,
        'X-VALR-SUB-ACCOUNT-ID': CMS2_ID,
    }
    
    resp = requests.get(f'{API_BASE}{path}', headers=headers)
    print(f"Balance response: {resp.status_code} - {resp.text[:200]}")
    if resp.status_code != 200:
        return {}
    
    balances = {}
    for b in resp.json():
        curr = b.get('currency', '')
        avail = float(b.get('available', 0) or 0)
        if avail > 0:
            balances[curr] = avail
    return balances

def get_ticker(pair):
    """Get current price from public orderbook."""
    resp = requests.get(f'{API_BASE}/v1/public/{pair}/orderbook')
    if resp.status_code != 200:
        print(f"    Orderbook request failed: {resp.status_code}")
        return None
    data = resp.json()
    # VALR uses capital letters: Bids/Asks
    bids = data.get('Bids', data.get('bids', []))
    if bids:
        return float(bids[0]['price'])
    # Fallback to mid price
    asks = data.get('Asks', data.get('asks', []))
    if asks and bids:
        return (float(bids[0]['price']) + float(asks[0]['price'])) / 2
    return None

def place_market_sell(base, quote, quantity, price_precision):
    """Place IOC sell order at current bid price."""
    pair = f"{base}{quote}"
    path = '/v1/orders'
    
    price = get_ticker(pair)
    if not price:
        print(f"  Could not get price for {pair}")
        return None
    
    # Discount price by 1% to ensure fill
    sell_price = price * 0.99
    sell_price = round(sell_price, price_precision)
    
    body = json.dumps({
        'currencyPair': pair,
        'side': 'SELL',
        'quantity': f"{quantity:.8f}",
        'price': f"{sell_price}",
        'timeInForce': 'IOC',
        'customerOrderId': f"liq-{base}-{int(time.time()*1000)}"
    })
    
    ts, sig = sign_request('POST', path, body, CMS2_ID)
    
    headers = {
        'X-VALR-API-KEY': api_key,
        'X-VALR-SIGNATURE': sig,
        'X-VALR-TIMESTAMP': ts,
        'X-VALR-SUB-ACCOUNT-ID': CMS2_ID,
        'Content-Type': 'application/json',
    }
    
    resp = requests.post(f'{API_BASE}{path}', headers=headers, data=body)
    print(f"    Order response: {resp.status_code} - {resp.text[:200]}")
    if resp.status_code == 202:
        return resp.json()
    return {'error': resp.text, 'status': resp.status_code}

def main():
    print(f"[{datetime.now().isoformat()}] Fetching CMS2 balances...")
    balances = get_balances()
    
    if not balances:
        print("No balances found or auth failed")
        return
    
    print(f"[{datetime.now().isoformat()}] CMS2 Balances:")
    for curr, amt in balances.items():
        print(f"  {curr}: {amt}")
    
    xstock_balances = {k: v for k, v in balances.items() if k in [x[0] for x in XSTOCK_PAIRS]}
    
    if not xstock_balances:
        print(f"\n[{datetime.now().isoformat()}] No xstock inventory found on CMS2")
        return
    
    print(f"\n[{datetime.now().isoformat()}] Placing liquidation orders...")
    
    for base, quote, price_prec in XSTOCK_PAIRS:
        if base not in xstock_balances:
            continue
        
        quantity = xstock_balances[base]
        print(f"\n  Selling {quantity} {base} for {quote}...")
        
        result = place_market_sell(base, quote, quantity, price_prec)
        print(f"    → {result}")
        
        time.sleep(0.5)
    
    print(f"\n[{datetime.now().isoformat()}] Liquidation complete!")

if __name__ == '__main__':
    main()
