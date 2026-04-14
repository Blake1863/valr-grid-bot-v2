#!/usr/bin/env python3
"""
Quote/Base Currency Replenisher — manages both USDT and base asset balances.

Quote assets: USDT, USDC only
Base assets: Everything else (including EURC, USDPC)

Triggers when:
- USDT/USDC drop below $5 → sells base assets to replenish
- Base assets too low for grid → buys more with USDT
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

# Minimum base asset inventory (cycles worth)
MIN_BASE_CYCLES = 5  # Keep at least 5 cycles of inventory
TARGET_BASE_CYCLES = 15  # Target this many cycles

# Approximate USD prices
USD_PRICES = {
    'USDT': 1.0, 'USDC': 1.0, 'USDPC': 1.0, 'EURC': 1.08,
    'JUP': 0.15, 'TRUMP': 10.0, 'SPYX': 600.0, 'VALR10': 50.0,
    'BITGOLD': 2000.0, 'MSTRX': 130.0, 'TSLAX': 350.0,
    'HOODX': 70.0, 'CRCLX': 100.0, 'COINX': 250.0, 'NVDAX': 120.0,
    'AVAX': 20.0, 'BNB': 600.0
}

# Enabled pairs from CM-Bot-Spot config
ENABLED_PAIRS = [
    "EURCUSDC", "JUPUSDT", "TRUMPUSDT", "SPYXUSDT", "VALR10USDT",
    "BITGOLDUSDT", "MSTRXUSDT", "TSLAXUSDT", "HOODXUSDT", "CRCLXUSDT",
    "COINXUSDT", "NVDAXUSDT", "USDPCUSDT"
]

# Estimated quantity per cycle (from bot config/observations)
QUANTITY_PER_CYCLE = {
    'EURC': 0.85, 'JUP': 5.0, 'TRUMP': 1.0, 'SPYX': 0.003, 'VALR10': 0.01,
    'BITGOLD': 0.003, 'MSTRX': 0.008, 'TSLAX': 0.004, 'HOODX': 0.013,
    'CRCLX': 0.015, 'COINX': 0.006, 'NVDAX': 0.006, 'USDPC': 1.0,
    'AVAX': 0.05, 'BNB': 0.003
}

# Tick sizes for pairs (from VALR API)
TICK_SIZES = {
    'TRUMPUSDT': 0.001, 'SPYXUSDT': 0.01, 'VALR10USDT': 0.01,
    'BITGOLDUSDT': 0.1, 'MSTRXUSDT': 0.01, 'TSLAXUSDT': 0.01,
    'HOODXUSDT': 0.01, 'CRCLXUSDT': 0.01, 'COINXUSDT': 0.01,
    'NVDAXUSDT': 0.01, 'JUPUSDT': 0.0001, 'AVAXUSDT': 0.001,
    'BNBUSDT': 0.01, 'EURCUSDC': 0.0001, 'USDPCUSDT': 0.01
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
    # Use hardcoded tick sizes (API calls often fail)
    tick = TICK_SIZES.get(pair, 0.01)
    
    # Try to get real prices from API
    try:
        req = urllib.request.Request(f"https://api.valr.com/v1/public/{pair}")
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            bids = data.get('Bids', data.get('bids', []))
            asks = data.get('Asks', data.get('asks', []))
            bid_price = float(bids[0]['price']) if bids else None
            ask_price = float(asks[0]['price']) if asks else None
            mid_price = (bid_price + ask_price) / 2 if bid_price and ask_price else None
            return bid_price, ask_price, mid_price, tick
    except:
        pass
    
    # Fallback to estimated prices
    price = USD_PRICES.get(pair.replace('USDT', '').replace('USDC', ''), 1.0)
    return None, None, price, tick

def place_limit_order(subaccount_id, pair, side, quantity, price, time_in_force="GTC"):
    """Place limit order via REST API."""
    path = "/v1/orders/limit"
    # Format price to avoid floating point issues - use 8 decimal places max
    price_str = f"{price:.8f}".rstrip('0').rstrip('.')
    body_dict = {
        "pair": pair,
        "side": side,
        "quantity": f"{quantity:.8f}",
        "price": price_str,
        "timeInForce": time_in_force,
        "customerOrderId": f"replenish-{pair}-{side}-{int(time.time())}"
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
    """Get list of base assets (non-quote) with available balances."""
    assets = []
    for bal in balances:
        curr = bal['currency']
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
    if currency == 'EURC':
        return 'EURCUSDC'
    elif currency == 'USDPC':
        return 'USDPCUSDT'
    else:
        return f"{currency}USDT"

def get_base_currency(pair):
    """Extract base currency from trading pair."""
    if pair.endswith('USDC'):
        return pair.replace('USDC', '')
    elif pair.endswith('USDT'):
        return pair.replace('USDT', '')
    return pair

def analyze_base_inventory(balances, account_name):
    """Analyze base asset inventory and identify shortages."""
    print(f"\n📦 {account_name} - Base Asset Inventory Check")
    print(f"{'='*80}")
    
    shortages = []
    
    for pair in ENABLED_PAIRS:
        base_curr = get_base_currency(pair)
        qty_per_cycle = QUANTITY_PER_CYCLE.get(base_curr, 0.01)
        min_qty = qty_per_cycle * MIN_BASE_CYCLES
        target_qty = qty_per_cycle * TARGET_BASE_CYCLES
        
        # Find balance
        avail = 0
        for bal in balances:
            if bal['currency'] == base_curr:
                avail = float(bal.get('available', 0))
                break
        
        cycles_available = avail / qty_per_cycle if qty_per_cycle > 0 else 0
        
        if cycles_available < MIN_BASE_CYCLES:
            shortfall_qty = target_qty - avail
            shortages.append({
                'currency': base_curr,
                'pair': pair,
                'available': avail,
                'min_needed': min_qty,
                'target_qty': target_qty,
                'shortfall': shortfall_qty,
                'cycles': cycles_available,
                'price_usd': USD_PRICES.get(base_curr, 1.0)
            })
            status = "⚠️ LOW" if cycles_available < MIN_BASE_CYCLES else "✅"
            print(f"   {status} {base_curr:>10}: {avail:>12.6f} ({cycles_available:>5.1f} cycles) - need {min_qty:.6f} min")
        else:
            print(f"   ✅ {base_curr:>10}: {avail:>12.6f} ({cycles_available:>5.1f} cycles)")
    
    return shortages

def main():
    print("="*80)
    print("💰 Quote/Base Currency Replenisher")
    print("   Quote assets: USDT, USDC | Base assets: Everything else")
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
        print(f"📋 {account_name}")
        print(f"{'='*80}")
        
        # === CHECK QUOTE ASSETS ===
        usdt_balance = get_quote_balance_usd(balances, 'USDT')
        usdc_balance = get_quote_balance_usd(balances, 'USDC')
        
        usdt_low = usdt_balance < MIN_BALANCE_USD
        usdc_low = usdc_balance < MIN_BALANCE_USD
        
        print(f"\n💵 Quote Assets:")
        print(f"   {'⚠️ LOW' if usdt_low else '✅ OK'} USDT: ${usdt_balance:>8.2f} (min: ${MIN_BALANCE_USD:.2f})")
        print(f"   {'⚠️ LOW' if usdc_low else '✅ OK'} USDC: ${usdc_balance:>8.2f} (min: ${MIN_BALANCE_USD:.2f})")
        
        # Calculate USDT shortfall
        usdt_shortfall = 0.0
        if usdt_low:
            usdt_shortfall = TARGET_BALANCE_USD - usdt_balance
            print(f"   → USDT shortfall: ${usdt_shortfall:.2f}")
        
        # === CHECK BASE ASSETS ===
        shortages = analyze_base_inventory(balances, account_name)
        
        # === REPLENISHMENT ACTIONS ===
        print(f"\n🔄 Replenishment Actions:")
        
        # 1. If USDT is low, sell base assets
        if usdt_shortfall > 0:
            print(f"\n   💸 USDT low - selling base assets...")
            base_assets = get_base_assets(balances)
            if not base_assets:
                print(f"      ⏭️  No base assets to sell")
            else:
                # Sort by USD value, exclude shortage items if possible
                base_assets.sort(key=lambda x: x['available'] * x['price_usd'], reverse=True)
                
                # Find assets that aren't in shortage
                shortage_currencies = set(s['currency'] for s in shortages)
                sellable = [a for a in base_assets if a['currency'] not in shortage_currencies]
                
                # If no non-shortage assets, use largest holdings anyway
                if not sellable:
                    sellable = base_assets[:3]  # Top 3 largest
                
                remaining = usdt_shortfall
                for asset in sellable:
                    if remaining <= 0:
                        break
                    asset_value = asset['available'] * asset['price_usd']
                    # Keep 50% reserve when selling for USDT
                    sell_pct = min(0.50, remaining / asset_value) if asset_value > 0 else 0
                    sell_qty = asset['available'] * sell_pct
                    sell_value = sell_qty * asset['price_usd']
                    
                    if sell_value >= 0.50:
                        pair = get_trading_pair(asset['currency'])
                        bid, ask, mid, tick = get_price_and_tick(pair)
                        # Use VERY aggressive pricing for sells - must be below bid to fill
                        # IOC sells need to hit existing bids, so price 2-3% below bid
                        if bid:
                            # Avoid floating point errors: calculate in integer ticks
                            ticks = int((bid * 0.97) / tick + 0.5)
                            sell_price = ticks * tick
                            print(f"         (Bid: ${bid:.4f}, Selling @ ${sell_price:.4f} = {((sell_price/bid)-1)*100:.1f}% below bid)")
                        else:
                            ticks = int((mid * 0.97) / tick + 0.5)
                            sell_price = ticks * tick
                        
                        print(f"      Selling {sell_qty:.6f} {asset['currency']} @ ${sell_price:.4f} via {pair}...")
                        success, result = place_limit_order(subaccount_id, pair, "SELL", sell_qty, sell_price, "IOC")
                        if success:
                            print(f"         ✅ Order {result[:24]} placed")
                            remaining -= sell_value
                        else:
                            print(f"         ❌ Failed: {result}")
                        time.sleep(0.3)
        
        # 2. If base assets are low, buy them with USDT
        if shortages:
            print(f"\n   📦 Base assets low - buying inventory...")
            
            # Calculate total USDT needed
            total_needed = sum(s['shortfall'] * s['price_usd'] for s in shortages)
            available_usdt = usdt_balance
            
            # Only buy if we have enough USDT (keep min balance)
            usable_usdt = max(0, available_usdt - MIN_BALANCE_USD)
            
            if usable_usdt < 5:
                print(f"      ⏭️  Insufficient USDT (${available_usdt:.2f}) - need ${total_needed:.2f}")
            else:
                print(f"      Available USDT: ${usable_usdt:.2f} | Need: ${total_needed:.2f}")
                
                # Prioritize by severity (lowest cycles first)
                shortages.sort(key=lambda x: x['cycles'])
                
                remaining_usdt = usable_usdt
                for shortage in shortages:
                    if remaining_usdt <= 0:
                        break
                    
                    buy_qty = shortage['shortfall']
                    buy_value = buy_qty * shortage['price_usd']
                    
                    # Cap purchase to available USDT
                    if buy_value > remaining_usdt:
                        buy_qty = remaining_usdt / shortage['price_usd']
                        buy_value = remaining_usdt
                    
                    pair = shortage['pair']
                    bid, ask, mid, tick = get_price_and_tick(pair)
                    
                    # Use VERY aggressive pricing for buys - must be above ask to fill
                    # IOC buys need to hit existing asks, so price 2-3% above ask
                    if ask:
                        # Avoid floating point errors: calculate in integer ticks
                        ticks = int((ask * 1.03) / tick + 0.5)
                        buy_price = ticks * tick
                        print(f"         (Ask: ${ask:.4f}, Buying @ ${buy_price:.4f} = {((buy_price/ask)-1)*100:.1f}% above ask)")
                    else:
                        ticks = int((mid * 1.03) / tick + 0.5)
                        buy_price = ticks * tick
                    
                    print(f"      Buying {buy_qty:.6f} {shortage['currency']} @ ${buy_price:.4f} via {pair}...")
                    success, result = place_limit_order(subaccount_id, pair, "BUY", buy_qty, buy_price, "IOC")
                    if success:
                        print(f"         ✅ Order {result[:24]} placed")
                        remaining_usdt -= buy_value
                    else:
                        print(f"         ❌ Failed: {result}")
                    time.sleep(0.3)
        
        print(f"\n   ✅ {account_name} replenishment pass complete")
    
    print("\n" + "="*80)
    print("✅ Full replenishment pass complete")
    print("="*80)

if __name__ == "__main__":
    main()
