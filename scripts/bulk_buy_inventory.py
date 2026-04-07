#!/usr/bin/env python3
"""
Bulk Buy Inventory — buys additional base assets across all active pairs.

Adds ~2x current buffer for sustained trading.
"""

import sys
import json
import hmac
import hashlib
import time
import urllib.request
import urllib.error

# Load creds from .env
with open("/home/admin/.openclaw/workspace/bots/cm-bot-v2/.env") as f:
    creds = {}
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, val = line.split('=', 1)
            creds[key.strip()] = val.strip()

API_KEY = creds['MAIN_API_KEY']
API_SECRET = creds['MAIN_API_SECRET']
CMS1_ID = "1483815480334401536"
CMS2_ID = "1483815498551132160"

# Pairs to buy + target additional quantity per account
# Format: (pair, base_currency, additional_qty_per_account)
BUY_LIST = [
    ("NVDAXUSDT", "NVDAX", 0.03),    # Add ~$5 per account
    ("TSLAXUSDT", "TSLAX", 0.02),    # Add ~$7 per account
    ("HOODXUSDT", "HOODX", 0.05),    # Add ~$3.50 per account
    ("CRCLXUSDT", "CRCLX", 0.03),    # Add ~$3.60 per account
    ("SPYXUSDT", "SPYX", 0.015),     # Add ~$10 per account
    ("BITGOLDUSDT", "BITGOLD", 0.02), # Add ~$40 per account
    ("MSTRXUSDT", "MSTRX", 0.05),    # Add ~$6.50 per account
    ("TRUMPUSDT", "TRUMP", 1.0),     # Add ~$10 per account
    ("COINXUSDT", "COINX", 0.015),   # Add ~$3.75 per account
    ("VALR10USDT", "VALR10", 0.05),  # Add ~$2.50 per account
    ("JUPUSDT", "JUP", 10.0),        # Add ~$1.50 per account
]

def timestamp_ms():
    return str(int(time.time() * 1000))

def sign_request(verb, path, body, subaccount_id=""):
    timestamp = timestamp_ms()
    message = f"{timestamp}{verb}{path}{body}{subaccount_id}"
    signature = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha512).hexdigest()
    return timestamp, signature

def get_price(pair):
    """Get current price from public orderbook."""
    try:
        req = urllib.request.Request(f"https://api.valr.com/v1/public/{pair}/orderbook")
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            asks = data.get('Asks', data.get('asks', []))
            if asks:
                return float(asks[0]['price'])
    except:
        pass
    return None

def place_buy_order(subaccount_id, pair, quantity, price):
    """Place IOC buy order via REST API."""
    path = "/v1/orders/limit"
    body_dict = {
        "pair": pair,
        "side": "BUY",
        "quantity": f"{quantity:.8f}",
        "price": f"{price}",
        "timeInForce": "IOC",
        "customerOrderId": f"bulkbuy-{pair}-{int(time.time())}"
    }
    body = json.dumps(body_dict)
    
    timestamp, signature = sign_request("POST", path, body, subaccount_id)
    
    headers = {
        "X-VALR-API-KEY": API_KEY,
        "X-VALR-SIGNATURE": signature,
        "X-VALR-TIMESTAMP": timestamp,
        "X-VALR-SUB-ACCOUNT-ID": subaccount_id,
        "Content-Type": "application/json",
    }
    
    req = urllib.request.Request("https://api.valr.com" + path, data=body.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode())
            return True, result.get('id', 'unknown')
    except urllib.error.HTTPError as e:
        return False, e.read().decode()[:200]

def round_price(price):
    """Round price to correct tick size."""
    if price >= 100:
        return round(price / 0.01) * 0.01
    elif price >= 1:
        return round(price / 0.001) * 0.001
    else:
        return round(price / 0.0001) * 0.0001

def main():
    print("="*80)
    print("🛒 Bulk Buy Base Inventory")
    print("="*80)
    
    buys_to_execute = []
    total_estimate = 0.0
    
    print("\n📊 Building buy list...")
    
    for pair, base_currency, add_qty in BUY_LIST:
        # Get current price
        price = get_price(pair)
        if not price:
            print(f"⚠️  {pair}: Could not fetch price, skipping")
            continue
        
        cost_per_account = add_qty * price
        total_cost = cost_per_account * 2  # Both accounts
        
        print(f"   {base_currency:>8}: +{add_qty:>10.6f} @ ${price:>8.2f} = ${total_cost:>8.2f} total")
        
        buys_to_execute.append((CMS1_ID, "CMS1", pair, base_currency, add_qty, price))
        buys_to_execute.append((CMS2_ID, "CMS2", pair, base_currency, add_qty, price))
        total_estimate += total_cost
    
    print(f"\n💰 Estimated total: ${total_estimate:.2f}")
    
    if total_estimate > 200:
        print(f"\n⚠️  Warning: High total cost. Confirm you want to proceed.")
    
    print("\n" + "="*80)
    print("🛒 Executing buy orders...")
    print("="*80)
    
    success_count = 0
    fail_count = 0
    
    for subaccount_id, account_name, pair, base_currency, qty, price in buys_to_execute:
        # Round price to tick size, add 2% buffer for fill
        buy_price = round_price(price * 1.02)
        
        print(f"\n   {account_name}: Buying {qty:.6f} {base_currency} @ ${buy_price:.4f}...")
        success, result = place_buy_order(subaccount_id, pair, qty, buy_price)
        if success:
            print(f"      ✅ Order {result[:24]} placed")
            success_count += 1
        else:
            print(f"      ❌ Failed: {result}")
            fail_count += 1
        time.sleep(0.3)
    
    print("\n" + "="*80)
    print(f"✅ Bulk buy complete: {success_count} succeeded, {fail_count} failed")
    print("="*80)

if __name__ == "__main__":
    main()
