#!/usr/bin/env python3
"""
Quote Currency Replenisher — sells base assets to replenish USDT/USDC balances.

Quote assets: USDT, USDC only
Base assets: Everything else (including EURC, USDPC)

Triggers when USDT or USDC drop below $5. Sells base assets to replenish.
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

# Minimum balance per quote asset (USD equivalent)
MIN_BALANCE_USD = 5.0
TARGET_BALANCE_USD = 50.0  # Replenish to this level

# Approximate USD prices
USD_PRICES = {
    'USDT': 1.0, 'USDC': 1.0, 'USDPC': 1.0, 'EURC': 1.08,
    'JUP': 0.15, 'TRUMP': 10.0, 'SPYX': 600.0, 'VALR10': 50.0,
    'BITGOLD': 2000.0, 'MSTRX': 130.0, 'TSLAX': 350.0,
    'HOODX': 70.0, 'CRCLX': 100.0, 'COINX': 250.0, 'NVDAX': 120.0,
    'AVAX': 20.0, 'BNB': 600.0
}

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

def get_price_and_tick(pair):
    """Get current price and tick size from public orderbook/pair info."""
    try:
        req = urllib.request.Request(f"https://api.valr.com/v1/public/{pair}")
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            tick_str = data.get('tickSize', '0.01')
            tick = float(tick_str)
            bids = data.get('Bids', data.get('bids', []))
            if bids:
                price = float(bids[0]['price'])
                return price, tick
    except:
        pass
    price = USD_PRICES.get(pair.replace('USDT', '').replace('USDC', ''), 1.0)
    if price >= 100:
        tick = 0.01
    elif price >= 1:
        tick = 0.001
    else:
        tick = 0.0001
    return price, tick

def place_sell_order(subaccount_id, pair, quantity, price):
    """Place IOC sell order via REST API."""
    path = "/v1/orders/limit"
    body_dict = {
        "pair": pair,
        "side": "SELL",
        "quantity": f"{quantity:.8f}",
        "price": f"{price}",
        "timeInForce": "IOC",
        "customerOrderId": f"replenish-{pair}-{int(time.time())}"
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

def get_base_assets(balances):
    """Get list of base assets (non-quote) with available balances.
    
    Quote assets are: USDT, USDC only
    Everything else (EURC, USDPC, etc.) is a base asset that can be sold.
    """
    assets = []
    for bal in balances:
        curr = bal['currency']
        # Only USDT and USDC are quote assets - everything else is sellable base
        if curr not in ['USDT', 'USDC', 'ZAR']:
            avail = float(bal.get('available', 0))
            if avail > 0.0001:
                assets.append({
                    'currency': curr,
                    'available': avail,
                    'price_usd': USD_PRICES.get(curr, 1.0)
                })
    return assets

def get_quote_balance_usd(balances, currency):
    """Get USD value of a specific quote asset."""
    for bal in balances:
        if bal['currency'] == currency:
            return float(bal.get('available', 0)) * USD_PRICES.get(currency, 1.0)
    return 0.0

def get_trading_pair(currency):
    """Determine the correct trading pair for a base asset."""
    # EURC trades against USDC
    if currency == 'EURC':
        return 'EURCUSDC'
    # USDPC trades against USDT
    elif currency == 'USDPC':
        return 'USDPCUSDT'
    # Everything else trades against USDT
    else:
        return f"{currency}USDT"

def main():
    print("="*80)
    print("💰 Quote Currency Replenisher")
    print("   Quote assets: USDT, USDC only")
    print("   Base assets: Everything else (EURC, USDPC, tokens)")
    print("="*80)
    
    # Fetch balances
    print("\n📊 Fetching balances...")
    cms1_balances = get_balances(CMS1_ID)
    cms2_balances = get_balances(CMS2_ID)
    
    accounts = [
        ("CMS1", CMS1_ID, cms1_balances),
        ("CMS2", CMS2_ID, cms2_balances),
    ]
    
    for account_name, subaccount_id, balances in accounts:
        print(f"\n{'='*80}")
        print(f"📋 {account_name} - Quote Asset Check")
        print(f"{'='*80}")
        
        # Check USDT and USDC only (the real quote assets)
        usdt_balance = get_quote_balance_usd(balances, 'USDT')
        usdc_balance = get_quote_balance_usd(balances, 'USDC')
        
        usdt_low = usdt_balance < MIN_BALANCE_USD
        usdc_low = usdc_balance < MIN_BALANCE_USD
        
        print(f"   {'⚠️ LOW' if usdt_low else '✅ OK'} USDT: ${usdt_balance:>8.2f} (min: ${MIN_BALANCE_USD:.2f})")
        print(f"   {'⚠️ LOW' if usdc_low else '✅ OK'} USDC: ${usdc_balance:>8.2f} (min: ${MIN_BALANCE_USD:.2f})")
        
        if not usdt_low and not usdc_low:
            print(f"   ✅ Both quote assets above minimum")
            continue
        
        # Calculate shortfall
        shortfall = 0.0
        if usdt_low:
            shortfall += TARGET_BALANCE_USD - usdt_balance
        if usdc_low:
            shortfall += TARGET_BALANCE_USD - usdc_balance
        
        print(f"\n🔄 Replenishment needed - Shortfall: ${shortfall:.2f}")
        
        # Get available base assets
        base_assets = get_base_assets(balances)
        if not base_assets:
            print(f"   ⏭️  No base assets to sell")
            continue
        
        # Sort by USD value (sell largest holdings first)
        base_assets.sort(key=lambda x: x['available'] * x['price_usd'], reverse=True)
        
        print(f"\n   Available base assets to sell:")
        for asset in base_assets[:10]:
            value = asset['available'] * asset['price_usd']
            pair = get_trading_pair(asset['currency'])
            print(f"      {asset['currency']:>10}: {asset['available']:>12.6f} @ ${asset['price_usd']:>8.4f} = ${value:>8.2f} → {pair}")
        
        # Calculate how much to sell of each asset
        remaining_shortfall = shortfall
        sells_to_execute = []
        
        for asset in base_assets:
            if remaining_shortfall <= 0:
                break
            
            asset_value = asset['available'] * asset['price_usd']
            
            # Keep 20% reserve
            sell_pct = min(0.80, remaining_shortfall / asset_value) if asset_value > 0 else 0
            sell_qty = asset['available'] * sell_pct
            sell_value = sell_qty * asset['price_usd']
            
            if sell_value >= 0.50:  # Minimum $0.50 per sell
                pair = get_trading_pair(asset['currency'])
                sells_to_execute.append((pair, sell_qty, asset['price_usd']))
                remaining_shortfall -= sell_value
                print(f"   → Sell {sell_qty:.6f} {asset['currency']} (~${sell_value:.2f}) via {pair}")
        
        if not sells_to_execute:
            print(f"   ⏭️  No sells to execute (assets too small)")
            continue
        
        # Execute sells
        print(f"\n   Executing {len(sells_to_execute)} sell orders...")
        for pair, qty, est_price in sells_to_execute:
            price, tick = get_price_and_tick(pair)
            aggressive_price = price * 0.98
            aggressive_price = round(aggressive_price / tick) * tick
            
            display_currency = pair.replace('USDT', '').replace('USDC', '')
            print(f"      Selling {qty:.6f} {display_currency} @ ~${aggressive_price:.4f} (tick: {tick})...")
            success, result = place_sell_order(subaccount_id, pair, qty, aggressive_price)
            if success:
                print(f"         ✅ Order {result[:24]} placed")
            else:
                print(f"         ❌ Failed: {result}")
            time.sleep(0.5)
        
        print(f"\n   {account_name} replenishment complete")
    
    print("\n" + "="*80)
    print("✅ Replenishment pass complete")
    print("="*80)

if __name__ == "__main__":
    main()
