#!/usr/bin/env python3
"""
Quote/Base Currency Replenisher — manages both USDT and base asset balances.

Uses SIMPLE orders (market execution) for reliable fills at VALR quoted prices.
No need to calculate tick sizes or bid/ask spreads.
"""

import json
import hmac
import hashlib
import time
import urllib.request

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
TARGET_BALANCE_USD = 50.0

# Minimum base asset inventory (cycles worth)
MIN_BASE_CYCLES = 5
TARGET_BASE_CYCLES = 10

# Approximate USD prices (fallback only)
USD_PRICES = {
    'USDT': 1.0, 'USDC': 1.0, 'USDPC': 1.0, 'EURC': 1.08,
    'JUP': 0.15, 'TRUMP': 2.85, 'SPYX': 600.0, 'VALR10': 50.0,
    'BITGOLD': 2000.0, 'MSTRX': 130.0, 'TSLAX': 350.0,
    'HOODX': 70.0, 'CRCLX': 100.0, 'COINX': 250.0, 'NVDAX': 120.0,
    'AVAX': 20.0, 'BNB': 600.0
}

ENABLED_PAIRS = [
    "EURCUSDC", "JUPUSDT", "TRUMPUSDT", "SPYXUSDT", "VALR10USDT",
    "BITGOLDUSDT", "MSTRXUSDT", "TSLAXUSDT", "HOODXUSDT", "CRCLXUSDT",
    "COINXUSDT", "NVDAXUSDT", "USDPCUSDT"
]

QUANTITY_PER_CYCLE = {
    'EURC': 0.85, 'JUP': 5.0, 'TRUMP': 1.0, 'SPYX': 0.003, 'VALR10': 0.01,
    'BITGOLD': 0.003, 'MSTRX': 0.008, 'TSLAX': 0.004, 'HOODX': 0.013,
    'CRCLX': 0.015, 'COINX': 0.006, 'NVDAX': 0.006, 'USDPC': 1.0,
    'AVAX': 0.05, 'BNB': 0.003
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

def get_market_prices():
    """Fetch live market prices from VALR market summary."""
    try:
        req = urllib.request.Request("https://api.valr.com/v1/public/marketsummary")
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            prices = {}
            for item in data:
                pair = item.get('currencyPair', '')
                last_price = item.get('lastTradedPrice', 0)
                if last_price:
                    prices[pair] = float(last_price)
            return prices
    except Exception as e:
        print(f"   ⚠️  Could not fetch market summary: {e}")
        return {}

def place_market_order(subaccount_id, pair, side, amount, is_base_amount=True):
    """Place MARKET order at best available price.
    
    MARKET orders:
    - Execute immediately at best available price
    - IOC (Immediate or Cancel) - partial fills allowed
    - Use baseAmount for SELL, quoteAmount for BUY
    """
    path = "/v2/orders/market"
    body_dict = {
        "side": side,
        "pair": pair,
    }
    # For SELL: specify baseAmount (the asset you're selling)
    # For BUY: specify quoteAmount (the USDT you're spending)
    if is_base_amount:
        body_dict["baseAmount"] = f"{amount:.8f}"
    else:
        body_dict["quoteAmount"] = f"{amount:.8f}"
    
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

def get_quote_currency(pair):
    """Get the quote currency for a trading pair."""
    if pair.endswith('USDC'):
        return 'USDC'
    elif pair.endswith('USDT'):
        return 'USDT'
    elif pair.endswith('ZAR'):
        return 'ZAR'
    else:
        return 'USDT'  # default

def get_base_currency(pair):
    """Extract base currency from trading pair."""
    if pair.endswith('USDC'):
        return pair.replace('USDC', '')
    elif pair.endswith('USDT'):
        return pair.replace('USDT', '')
    return pair

def analyze_base_inventory(balances, account_name, market_prices):
    """Analyze base asset inventory and identify shortages."""
    print(f"\n📦 {account_name} - Base Asset Inventory Check")
    print(f"{'='*80}")
    
    shortages = []
    
    for pair in ENABLED_PAIRS:
        base_curr = get_base_currency(pair)
        qty_per_cycle = QUANTITY_PER_CYCLE.get(base_curr, 0.01)
        min_qty = qty_per_cycle * MIN_BASE_CYCLES
        target_qty = qty_per_cycle * TARGET_BASE_CYCLES
        
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
    print("   Using MARKET orders for market execution")
    print("="*80)
    
    # Fetch market prices first
    print("\n📊 Fetching market prices...")
    market_prices = get_market_prices()
    if market_prices:
        print(f"   ✅ Got prices for {len(market_prices)} pairs")
        if 'TRUMPUSDT' in market_prices:
            print(f"   TRUMPUSDT: ${market_prices['TRUMPUSDT']:.4f}")
    else:
        print(f"   ⚠️  Using fallback prices")
    
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
        
        usdt_shortfall = 0.0
        if usdt_low:
            usdt_shortfall = TARGET_BALANCE_USD - usdt_balance
            print(f"   → USDT shortfall: ${usdt_shortfall:.2f}")
        
        usdc_shortfall = 0.0
        if usdc_low:
            usdc_shortfall = TARGET_BALANCE_USD - usdc_balance
            print(f"   → USDC shortfall: ${usdc_shortfall:.2f}")
        
        # === CHECK BASE ASSETS ===
        shortages = analyze_base_inventory(balances, account_name, market_prices)
        
        # === REPLENISHMENT ACTIONS ===
        print(f"\n🔄 Replenishment Actions:")
        
        # 1. If USDT is low, sell base assets
        if usdt_shortfall > 0:
            print(f"\n   💸 USDT low - selling base assets...")
            base_assets = get_base_assets(balances)
            if not base_assets:
                print(f"      ⏭️  No base assets to sell")
            else:
                base_assets.sort(key=lambda x: x['available'] * x['price_usd'], reverse=True)
                
                # Exclude shortage items
                shortage_currencies = set(s['currency'] for s in shortages)
                
                # Sell from enabled pairs that have EXCESS (above target cycles)
                # This is the whole point - rotate surplus inventory for USDT
                sellable = []
                for asset in base_assets:
                    if asset['currency'] in shortage_currencies:
                        continue
                    
                    # Calculate cycles
                    qty_per_cycle = QUANTITY_PER_CYCLE.get(asset['currency'], 0.01)
                    cycles = asset['available'] / qty_per_cycle if qty_per_cycle > 0 else 0
                    
                    # Only sell if above target (we have surplus)
                    if cycles >= TARGET_BASE_CYCLES:
                        sellable.append({**asset, 'cycles': cycles})
                
                # Sort by value (sell largest surplus first)
                sellable.sort(key=lambda x: x['available'] * x['price_usd'], reverse=True)
                
                remaining = usdt_shortfall
                for asset in sellable:
                    if remaining <= 0:
                        break
                    asset_value = asset['available'] * asset['price_usd']
                    # Keep 50% reserve
                    sell_pct = min(0.50, remaining / asset_value) if asset_value > 0 else 0
                    sell_qty = asset['available'] * sell_pct
                    sell_value = sell_qty * asset['price_usd']
                    
                    if sell_value >= 0.50:
                        pair = get_trading_pair(asset['currency'])
                        # For MARKET sell: use baseAmount (selling the base asset)
                        print(f"      Selling {sell_qty:.6f} {asset['currency']} via {pair}...")
                        success, result = place_market_order(subaccount_id, pair, "SELL", sell_qty, is_base_amount=True)
                        if success:
                            print(f"         ✅ Order {result[:24]} placed")
                            remaining -= sell_value
                        else:
                            print(f"         ❌ Failed: {result}")
                        time.sleep(0.3)
        
        # 2. If base assets are low, buy them with correct quote currency
        if shortages:
            print(f"\n   📦 Base assets low - buying inventory...")
            
            # Separate shortages by quote currency needed
            usdc_shortages = [s for s in shortages if s['pair'].endswith('USDC')]
            usdt_shortages = [s for s in shortages if s['pair'].endswith('USDT')]
            
            # Process USDC purchases first (EURC etc)
            if usdc_shortages:
                total_usdc_needed = sum(s['shortfall'] * s['price_usd'] for s in usdc_shortages)
                usable_usdc = max(0, usdc_balance - MIN_BALANCE_USD)
                
                if usable_usdc < 5:
                    print(f"      ⏭️  Insufficient USDC (${usdc_balance:.2f}) - need ${total_usdc_needed:.2f}")
                else:
                    print(f"      Available USDC: ${usable_usdc:.2f} | Need: ${total_usdc_needed:.2f}")
                    usdc_shortages.sort(key=lambda x: x['cycles'])
                    
                    remaining_usdc = usable_usdc
                    for shortage in usdc_shortages:
                        if remaining_usdc <= 0:
                            break
                        
                        buy_qty = shortage['shortfall']
                        buy_value = buy_qty * shortage['price_usd']
                        
                        if buy_value > remaining_usdc:
                            buy_qty = remaining_usdc / shortage['price_usd']
                            buy_value = remaining_usdc
                        
                        pair = shortage['pair']
                        print(f"      Buying {shortage['currency']} with ${buy_value:.2f} USDC via {pair}...")
                        success, result = place_market_order(subaccount_id, pair, "BUY", buy_value, is_base_amount=False)
                        if success:
                            print(f"         ✅ Order {result[:24]} placed")
                            remaining_usdc -= buy_value
                        else:
                            print(f"         ❌ Failed: {result}")
                        time.sleep(0.3)
            
            # Process USDT purchases
            if usdt_shortages:
                total_usdt_needed = sum(s['shortfall'] * s['price_usd'] for s in usdt_shortages)
                usable_usdt = max(0, usdt_balance - MIN_BALANCE_USD)
                
                if usable_usdt < 5:
                    print(f"      ⏭️  Insufficient USDT (${usdt_balance:.2f}) - need ${total_usdt_needed:.2f}")
                else:
                    print(f"      Available USDT: ${usable_usdt:.2f} | Need: ${total_usdt_needed:.2f}")
                    usdt_shortages.sort(key=lambda x: x['cycles'])
                    
                    remaining_usdt = usable_usdt
                    for shortage in usdt_shortages:
                        if remaining_usdt <= 0:
                            break
                        
                        buy_qty = shortage['shortfall']
                        buy_value = buy_qty * shortage['price_usd']
                        
                        if buy_value > remaining_usdt:
                            buy_qty = remaining_usdt / shortage['price_usd']
                            buy_value = remaining_usdt
                        
                        pair = shortage['pair']
                        print(f"      Buying {shortage['currency']} with ${buy_value:.2f} USDT via {pair}...")
                        success, result = place_market_order(subaccount_id, pair, "BUY", buy_value, is_base_amount=False)
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
