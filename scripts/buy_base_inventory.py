#!/usr/bin/env python3
"""
Buy Base Inventory — purchases base assets for pairs with insufficient inventory.

Targets pairs that are failing due to low base asset holdings.
Buys enough inventory for ~20 cycles of trading.
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

# Pairs with chronically low inventory (from log analysis)
# Format: (pair, base_currency, target_per_account, current_total)
PROBLEM_PAIRS = [
    ("NVDAXUSDT", "NVDAX", 0.02, None),   # ~$120/share, target $2.40 per account
    ("TSLAXUSDT", "TSLAX", 0.01, None),   # ~$350/share, target $3.50 per account
    ("HOODXUSDT", "HOODX", 0.05, None),   # ~$70/share, target $3.50 per account
    ("CRCLXUSDT", "CRCLX", 0.02, None),   # ~$100/share, target $2.00 per account
    ("SPYXUSDT", "SPYX", 0.01, None),     # ~$600/share, target $6.00 per account
]

def timestamp_ms():
    return str(int(time.time() * 1000))

def sign_request(verb, path, body, subaccount_id=""):
    timestamp = timestamp_ms()
    message = f"{timestamp}{verb}{path}{body}{subaccount_id}"
    signature = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha512).hexdigest()
    return timestamp, signature

def get_balances(subaccount_id):
    timestamp, signature = sign_request("GET", "/v1/account/balances", "", subaccount_id)
    
    headers = {
        "X-VALR-API-KEY": API_KEY,
        "X-VALR-SIGNATURE": signature,
        "X-VALR-TIMESTAMP": timestamp,
        "X-VALR-SUB-ACCOUNT-ID": subaccount_id,
    }
    
    req = urllib.request.Request("https://api.valr.com/v1/account/balances", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode())

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
        "customerOrderId": f"buyinv-{pair}-{int(time.time())}"
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

def main():
    print("="*80)
    print("🛒 Buy Base Inventory for Problem Pairs")
    print("="*80)
    
    # Fetch current balances
    print("\n📊 Fetching current inventory...")
    cms1_balances = get_balances(CMS1_ID)
    cms2_balances = get_balances(CMS2_ID)
    
    # Build inventory dict
    cms1_inv = {b['currency']: float(b.get('available', 0)) for b in cms1_balances}
    cms2_inv = {b['currency']: float(b.get('available', 0)) for b in cms2_balances}
    
    # Check USDT balances (for buying)
    cms1_usdt = cms1_inv.get('USDT', 0)
    cms2_usdt = cms2_inv.get('USDT', 0)
    
    print(f"\n   CMS1 USDT: ${cms1_usdt:.2f}")
    print(f"   CMS2 USDT: ${cms2_usdt:.2f}")
    
    buys_to_execute = []
    
    print("\n" + "="*80)
    print("Inventory Analysis")
    print("="*80)
    
    for pair, base_currency, target_per_account, _ in PROBLEM_PAIRS:
        cms1_have = cms1_inv.get(base_currency, 0)
        cms2_have = cms2_inv.get(base_currency, 0)
        total_have = cms1_have + cms2_have
        
        # Get current price
        price = get_price(pair)
        if not price:
            print(f"\n⚠️  {pair}: Could not fetch price, skipping")
            continue
        
        # Calculate how much to buy
        cms1_need = max(0, target_per_account - cms1_have)
        cms2_need = max(0, target_per_account - cms2_have)
        
        cms1_cost = cms1_need * price
        cms2_cost = cms2_need * price
        total_cost = cms1_cost + cms2_cost
        
        print(f"\n{pair} (current price: ${price:.2f}):")
        print(f"   CMS1: Have {cms1_have:.6f}, Need {cms1_need:.6f} (${cms1_cost:.2f})")
        print(f"   CMS2: Have {cms2_have:.6f}, Need {cms2_need:.6f} (${cms2_cost:.2f})")
        print(f"   Total cost: ${total_cost:.2f}")
        
        if cms1_need > 0:
            buys_to_execute.append((CMS1_ID, "CMS1", pair, base_currency, cms1_need, price))
        if cms2_need > 0:
            buys_to_execute.append((CMS2_ID, "CMS2", pair, base_currency, cms2_need, price))
    
    if not buys_to_execute:
        print("\n✅ All pairs have sufficient inventory")
        return
    
    total_estimate = sum(qty * price for _, _, _, _, qty, price in buys_to_execute)
    
    print("\n" + "="*80)
    print(f"Summary: {len(buys_to_execute)} buys, estimated total: ${total_estimate:.2f}")
    print("="*80)
    
    if total_estimate > cms1_usdt + cms2_usdt:
        print(f"\n⚠️  Warning: Total cost (${total_estimate:.2f}) exceeds available USDT (${cms1_usdt + cms2_usdt:.2f})")
        print("   Consider funding accounts with more USDT first")
    
    # Execute buys
    print("\n🛒 Executing buy orders...")
    
    for subaccount_id, account_name, pair, base_currency, qty, price in buys_to_execute:
        if qty <= 0:
            continue
        
        # Round up to reasonable precision based on pair
        # VALR tick sizes: $100+ = 0.01, $1-100 = 0.001, <$1 = 0.0001
        if price >= 100:
            buy_price = round(price * 1.02 / 0.01) * 0.01  # 2% above, rounded to 0.01
            qty_rounded = round(qty, 6)
        elif price >= 1:
            buy_price = round(price * 1.02 / 0.001) * 0.001  # 2% above, rounded to 0.001
            qty_rounded = round(qty, 6)
        else:
            buy_price = round(price * 1.02 / 0.0001) * 0.0001  # 2% above, rounded to 0.0001
            qty_rounded = round(qty, 8)
        
        # Ensure minimum order value of $0.50
        min_qty = 0.50 / price
        if qty_rounded < min_qty:
            qty_rounded = min_qty * 1.1  # Add 10% buffer
            qty_rounded = round(qty_rounded, 6)
        
        print(f"\n   {account_name}: Buying {qty_rounded:.6f} {base_currency} @ ~${buy_price:.4f}...")
        success, result = place_buy_order(subaccount_id, pair, qty_rounded, buy_price)
        if success:
            print(f"      ✅ Order {result[:24]} placed")
        else:
            print(f"      ❌ Failed: {result}")
        time.sleep(0.5)
    
    print("\n" + "="*80)
    print("✅ Inventory purchase complete")
    print("="*80)
    print("\n💡 Next steps:")
    print("   1. Wait for orders to fill (IOC - immediate)")
    print("   2. Run spot_rebalance_manual.py to distribute if needed")
    print("   3. Monitor bot logs for reduced failures")

if __name__ == "__main__":
    main()
