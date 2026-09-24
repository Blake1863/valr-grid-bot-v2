#!/usr/bin/env python3
"""
Quote/Base Currency Replenisher — manages USDT, USDC, and ZAR inventories.

Uses MARKET orders for reliable fills at VALR quoted prices.
Converts between quote currencies when one is low (USDT↔USDC, USDT↔ZAR, USDC↔ZAR).
Also replenishes base assets that are running low.
"""

import json
import hmac
import hashlib
import time
import urllib.request

# ── Configuration ─────────────────────────────────────────────────────────────

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

# Minimum/target balance per quote asset (USD equivalent)
MIN_BALANCE_USD = 20.0   # Floor per quote currency
TARGET_BALANCE_USD = 50.0

# ZAR exchange rate — will be overwritten by live API fetch on startup
ZAR_USD_RATE = 1.0 / 16.66  # ~16.66 ZAR per USD

# Minimum base asset inventory (cycles worth)
MIN_BASE_CYCLES = 5
TARGET_BASE_CYCLES = 10

# Approximate USD prices (fallback only, overwritten by live API)
USD_PRICES = {
    'USDT': 1.0, 'USDC': 1.0, 'USDPC': 1.0, 'EURC': 1.08,
    'JUP': 0.19, 'TRUMP': 2.85, 'SPYX': 600.0, 'VALR10': 60.0,
    'BITGOLD': 104.0, 'MSTRX': 175.0, 'TSLAX': 377.0,
    'HOODX': 75.0, 'CRCLX': 94.0, 'COINX': 250.0, 'NVDAX': 120.0,
    'AVAX': 85.0, 'BNB': 600.0, 'PUMP': 0.0018,
    'XAUT': 3300.0, 'LINK': 15.0, 'ETH': 2300.0, 'SOL': 165.0,
    'XRP': 2.0, 'DOGE': 0.15,
}

# All base assets managed across all three quote currencies
ALL_BASE_ASSETS = [
    # USDT pairs
    ('JUPUSDT', 'JUP'), ('TRUMPUSDT', 'TRUMP'), ('SPYXUSDT', 'SPYX'),
    ('VALR10USDT', 'VALR10'), ('BITGOLDUSDT', 'BITGOLD'), ('MSTRXUSDT', 'MSTRX'),
    ('TSLAXUSDT', 'TSLAX'), ('HOODXUSDT', 'HOODX'), ('CRCLXUSDT', 'CRCLX'),
    ('COINXUSDT', 'COINX'), ('NVDAXUSDT', 'NVDAX'), ('USDPCUSDT', 'USDPC'),
    ('PUMPUSDT', 'PUMP'), ('AVAXUSDT', 'AVAX'), ('BNBUSDT', 'BNB'),
    ('ETHUSDT', 'ETH'), ('LINKUSDT', 'LINK'), ('SOLUSDT', 'SOL'),
    ('XAUTUSDT', 'XAUT'), ('XRPUSDT', 'XRP'), ('DOGEUSDT', 'DOGE'),
    # USDC pairs
    ('EURCUSDC', 'EURC'),
    # ZAR pairs (for spot bot on CMS1/CMS2)
    ('BTCZAR', 'BTC'), ('ETHZAR', 'ETH'), ('XRPZAR', 'XRP'),
    ('SOLZAR', 'SOL'), ('AVAXZAR', 'AVAX'), ('BNBZAR', 'BNB'),
    ('LINKZAR', 'LINK'), ('USDCZAR', 'USDC'), ('USDTZAR', 'USDT'),
    ('XAUTZAR', 'XAUT'),
]

# Quantity per cycle for each base asset (how much is needed for one wash cycle)
QUANTITY_PER_CYCLE = {
    'EURC': 0.85, 'JUP': 5.0, 'TRUMP': 1.0, 'SPYX': 0.003, 'VALR10': 0.01,
    'BITGOLD': 0.003, 'MSTRX': 0.008, 'TSLAX': 0.004, 'HOODX': 0.013,
    'CRCLX': 0.015, 'COINX': 0.006, 'NVDAX': 0.006, 'USDPC': 1.0,
    'AVAX': 0.05, 'BNB': 0.003, 'ETH': 0.01, 'LINK': 0.5,
    'SOL': 0.1, 'XAUT': 0.003, 'XRP': 2.0, 'DOGE': 100.0,
    'PUMP': 150.0, 'BTC': 0.0003,
    # ZAR pairs — these use the same base asset quantities
    'BTC': 0.0003, 'ETH': 0.01, 'XRP': 2.0, 'SOL': 0.1,
    'AVAX': 0.05, 'BNB': 0.003, 'LINK': 0.5, 'USDC': 1.0, 'USDT': 1.0,
    'XAUT': 0.003,
}

# Pairs to actively manage for base inventory replenishment
# (ZAR pairs have their own cycle in the spot bot)
ENABLED_PAIRS = [
    "EURCUSDC", "JUPUSDT", "TRUMPUSDT", "SPYXUSDT", "VALR10USDT",
    "BITGOLDUSDT", "MSTRXUSDT", "TSLAXUSDT", "HOODXUSDT", "CRCLXUSDT",
    "COINXUSDT", "NVDAXUSDT", "USDPCUSDT",
    # ZAR pairs are managed by the spot bot's wash cycle
    "BTCZAR", "ETHZAR", "XRPZAR", "SOLZAR", "AVAXZAR", "BNBZAR",
    "LINKZAR", "USDCZAR", "USDTZAR", "XAUTZAR",
]

# VALR minimum order sizes per pair
MIN_ORDER_SIZES = {
    'PUMPUSDT': 135.0,   # 135 PUMP minimum
    'JUPUSDT': 0.45,
    'TRUMPUSDT': 0.02,
    'SPYXUSDT': 0.001,
    'VALR10USDT': 0.005,
    'BITGOLDUSDT': 0.005,
    'MSTRXUSDT': 0.001,
    'TSLAXUSDT': 0.001,
    'HOODXUSDT': 0.005,
    'CRCLXUSDT': 0.003,
    'COINXUSDT': 0.001,
    'NVDAXUSDT': 0.003,
    'EURCUSDC': 0.5,
    'ETHUSDT': 0.001,
    'LINKUSDT': 0.05,
    'SOLUSDT': 0.005,
    'XAUTUSDT': 0.0001,
    'BNBUSDT': 0.001,
    'AVAXUSDT': 0.01,
    'BTCZAR': 0.00001,
    'ETHZAR': 0.0002,
    'SOLZAR': 0.003,
    'XAUTZAR': 0.0002,
    'BNBZAR': 0.0008,
    'LINKZAR': 0.04,
    'AVAXZAR': 0.0128,
    'XRPZAR': 0.8,
    'DOGEZAR': 1.5,
    'USDCZAR': 0.5,
    'USDTZAR': 0.5,
}

# ── API helpers ───────────────────────────────────────────────────────────────

def timestamp_ms():
    return str(int(time.time() * 1000))

def sign_request(verb, path, body, subaccount_id=""):
    timestamp = timestamp_ms()
    message = f"{timestamp}{verb}{path}{body}{subaccount_id}"
    signature = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha512).hexdigest()
    return timestamp, signature

def api_request(method, path, body="", subaccount_id=""):
    """Generic API request with VALR authentication."""
    ts, sig = sign_request(method, path, body, subaccount_id)
    url = f"https://api.valr.com{path}"
    headers = {
        "X-VALR-API-KEY": API_KEY,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }
    if subaccount_id:
        headers["X-VALR-SUB-ACCOUNT-ID"] = subaccount_id

    req = urllib.request.Request(url, data=body.encode() if body else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return None, e.read().decode()[:200]

def get_balances(subaccount_id):
    result = api_request("GET", "/v1/account/balances", "", subaccount_id)
    if isinstance(result, tuple):
        # Error response
        return None
    return result

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

def get_orderbook_asks(pair, limit=1):
    """Get best ask(s) for a pair."""
    try:
        req = urllib.request.Request(f"https://api.valr.com/v1/public/{pair}/orderbook?limit={limit}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get('asks', [])
    except:
        return []

def place_market_order(subaccount_id, pair, side, amount, is_base_amount=True):
    """Place MARKET order at best available price."""
    path = "/v2/orders/market"
    body_dict = {"side": side, "pair": pair}
    if is_base_amount:
        body_dict["baseAmount"] = f"{amount:.8f}"
    else:
        body_dict["quoteAmount"] = f"{amount:.8f}"

    body = json.dumps(body_dict)
    ts, sig = sign_request("POST", path, body, subaccount_id)
    headers = {
        "X-VALR-API-KEY": API_KEY,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
        "X-VALR-SUB-ACCOUNT-ID": subaccount_id,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request("https://api.valr.com" + path, data=body.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return True, result.get('id', 'unknown')
    except urllib.error.HTTPError as e:
        return False, e.read().decode()[:200]

# ── Currency helpers ─────────────────────────────────────────────────────────

def get_usd_value(currency, amount, market_prices):
    """Convert an amount of any currency to USD using live prices or fallbacks."""
    if currency in ('USDT', 'USDC', 'USDPC'):
        return amount  # 1:1 USD

    if currency == 'ZAR':
        # Use live ZAR rate if available
        if market_prices and 'USDTZAR' in market_prices:
            rate = market_prices['USDTZAR']  # ZAR per USDT
            return amount / rate
        return amount * ZAR_USD_RATE

    # Try live market price first
    for quote in ('USDT', 'USDC', 'ZAR'):
        pair = f"{currency}{quote}"
        if market_prices and pair in market_prices:
            price = market_prices[pair]
            if quote == 'ZAR' and market_prices and 'USDTZAR' in market_prices:
                zar_usd = 1.0 / market_prices['USDTZAR']
                return amount * price * zar_usd
            return amount * price

    # Fallback to hardcoded price
    return amount * USD_PRICES.get(currency, 0)

def get_best_conversion_path(from_currency, to_currency):
    """Return (pair, side, is_base) for converting between quote currencies."""
    if from_currency == 'USDT' and to_currency == 'USDC':
        return ('USDTUSDC', 'SELL', True)   # Sell USDT, buy USDC
    if from_currency == 'USDC' and to_currency == 'USDT':
        return ('USDTUSDC', 'BUY', False)    # Buy USDT with USDC
    if from_currency == 'USDT' and to_currency == 'ZAR':
        return ('USDTZAR', 'SELL', True)     # Sell USDT, buy ZAR
    if from_currency == 'ZAR' and to_currency == 'USDT':
        return ('USDTZAR', 'BUY', False)     # Buy USDT with ZAR
    if from_currency == 'USDC' and to_currency == 'ZAR':
        return ('USDCZAR', 'SELL', True)     # Sell USDC, buy ZAR
    if from_currency == 'ZAR' and to_currency == 'USDC':
        return ('USDCZAR', 'BUY', False)     # Buy USDC with ZAR
    return None

def get_quote_currency(pair):
    """Get the quote currency for a trading pair."""
    if pair.endswith('USDC'):
        return 'USDC'
    elif pair.endswith('ZAR'):
        return 'ZAR'
    else:
        return 'USDT'

def get_base_currency(pair):
    """Extract base currency from trading pair."""
    if pair.endswith('USDC'):
        return pair[:-4]
    elif pair.endswith('USDT'):
        return pair[:-4]
    elif pair.endswith('ZAR'):
        return pair[:-3]
    return pair

# ── Analysis ─────────────────────────────────────────────────────────────────

def check_quote_balances(balances, account_name, market_prices):
    """Check all three quote currencies (USDT, USDC, ZAR)."""
    results = {}
    for currency in ('USDT', 'USDC', 'ZAR'):
        avail = 0.0
        for bal in balances:
            if bal['currency'] == currency:
                avail = float(bal.get('available', 0))
                break
        usd_value = get_usd_value(currency, avail, market_prices)
        results[currency] = {
            'available': avail,
            'usd_value': usd_value,
            'low': usd_value < MIN_BALANCE_USD,
            'shortfall': max(0, TARGET_BALANCE_USD - usd_value) if usd_value < MIN_BALANCE_USD else 0,
        }

    print(f"\n💵 Quote Assets ({account_name}):")
    for currency in ('USDT', 'USDC', 'ZAR'):
        r = results[currency]
        symbol = 'ZAR' if currency == 'ZAR' else '$'
        if currency == 'ZAR':
            display = f"R{r['available']:.2f}"
            usd_equiv = f"(~${r['usd_value']:.2f})"
        else:
            display = f"${r['available']:.2f}"
            usd_equiv = ""
        status = "⚠️ LOW" if r['low'] else "✅ OK"
        print(f"   {status} {currency}: {display} {usd_equiv} (min: ${MIN_BALANCE_USD:.2f})")
        if r['low']:
            print(f"   → {currency} shortfall: ${r['shortfall']:.2f} USD equivalent")

    return results

def analyze_base_inventory(balances, account_name, market_prices, quote_results):
    """Analyze base asset inventory and identify shortages per quote currency."""
    print(f"\n📦 {account_name} - Base Asset Inventory Check")
    print(f"{'='*80}")

    shortages = []

    for pair, base_curr in ALL_BASE_ASSETS:
        if pair not in ENABLED_PAIRS:
            continue

        qty_per_cycle = QUANTITY_PER_CYCLE.get(base_curr, 0.01)
        min_qty = qty_per_cycle * MIN_BASE_CYCLES
        target_qty = qty_per_cycle * TARGET_BASE_CYCLES

        # Find available balance
        avail = 0
        for bal in balances:
            if bal['currency'] == base_curr:
                avail = float(bal.get('available', 0))
                break

        cycles_available = avail / qty_per_cycle if qty_per_cycle > 0 else 0

        if cycles_available < MIN_BASE_CYCLES:
            shortfall_qty = target_qty - avail
            quote_curr = get_quote_currency(pair)
            # Check if the quote currency itself has enough to buy
            quote_usd = quote_results.get(quote_curr, {}).get('usd_value', 0)
            can_afford = quote_usd > MIN_BALANCE_USD

            shortages.append({
                'currency': base_curr,
                'pair': pair,
                'available': avail,
                'min_needed': min_qty,
                'target_qty': target_qty,
                'shortfall': shortfall_qty,
                'cycles': cycles_available,
                'quote_currency': quote_curr,
                'can_afford': can_afford,
                'price_usd': USD_PRICES.get(base_curr, 1.0)
            })
            status = "⚠️ LOW"
            print(f"   {status} {base_curr:>10}: {avail:>12.6f} ({cycles_available:>5.1f} cycles) - need {min_qty:.6f} min | via {quote_curr}")
        else:
            print(f"   ✅ {base_curr:>10}: {avail:>12.6f} ({cycles_available:>5.1f} cycles)")

    return shortages

# ── Replenishment ─────────────────────────────────────────────────────────────

def replenish_quote_currencies(balances, account_name, quote_results, subaccount_id):
    """Convert between quote currencies when one is low and others have surplus."""
    print(f"\n🔄 {account_name} - Quote Currency Replenishment")

    # Get available amounts for each quote currency
    available = {}
    for currency in ('USDT', 'USDC', 'ZAR'):
        avail = 0.0
        for bal in balances:
            if bal['currency'] == currency:
                avail = float(bal.get('available', 0))
                break
        available[currency] = avail

    # Find which currencies are low and which have surplus
    low_currencies = [c for c, r in quote_results.items() if r['low']]
    surplus_currencies = []
    for currency in ('USDT', 'USDC', 'ZAR'):
        if currency not in low_currencies:
            usd_value = quote_results[currency]['usd_value']
            surplus = usd_value - MIN_BALANCE_USD
            if surplus > 5:  # At least $5 surplus to justify conversion
                surplus_currencies.append((currency, surplus))

    if not low_currencies:
        print("   ✅ All quote currencies above minimum — no conversion needed")
        return

    for low_curr in low_currencies:
        shortfall = quote_results[low_curr]['shortfall']
        print(f"   {low_curr} needs ${shortfall:.2f} more")

        for surplus_curr, surplus_usd in surplus_currencies:
            path = get_best_conversion_path(surplus_curr, low_curr)
            if not path:
                continue

            pair, side, is_base = path
            convert_usd = min(surplus_usd * 0.5, shortfall)  # Use at most 50% of surplus

            if convert_usd < 1:
                continue

            print(f"   Converting ${convert_usd:.2f} {surplus_curr} → {low_curr} via {pair} ({side})...")

            # Determine order amount
            if is_base:
                # Selling surplus currency (baseAmount)
                if surplus_curr in ('USDT', 'USDC', 'USDPC'):
                    amount = convert_usd
                else:
                    amount = convert_usd / USD_PRICES.get(surplus_curr, 1)
            else:
                # Buying with surplus currency (quoteAmount)
                amount = convert_usd

            success, result = place_market_order(subaccount_id, pair, side, amount, is_base)
            if success:
                print(f"      ✅ Order {result[:24]} placed")
            else:
                print(f"      ❌ Failed: {result}")
            time.sleep(0.3)

def sell_base_for_quote(balances, account_name, quote_results, subaccount_id, market_prices):
    """Sell excess base assets to replenish low quote currencies."""
    low_currencies = [c for c, r in quote_results.items() if r['low']]
    if not low_currencies:
        return

    print(f"\n💸 {account_name} - Selling surplus base assets for quote replenishment")

    # Build a map of available base assets
    base_map = {}
    for bal in balances:
        curr = bal['currency']
        if curr not in ('USDT', 'USDC', 'ZAR', 'EURC'):
            avail = float(bal.get('available', 0))
            if avail > 0.0001:
                base_map[curr] = avail

    for low_curr in low_currencies:
        shortfall = quote_results[low_curr]['shortfall']
        remaining = shortfall
        print(f"   Target: ${remaining:.2f} more {low_curr}")

        for base_curr, avail_qty in sorted(base_map.items(), key=lambda x: x[1] * USD_PRICES.get(x[0], 0), reverse=True):
            if remaining <= 0:
                break

            pair = f"{base_curr}{low_curr}"
            # Check if pair exists and supports market orders
            value_usd = avail_qty * USD_PRICES.get(base_curr, 0)

            if value_usd < 1:
                continue

            # Sell up to 50% of surplus (keep 50% reserve)
            sell_pct = 0.50
            sell_value = value_usd * sell_pct
            sell_qty = avail_qty * sell_pct

            if sell_value < remaining * 0.3:  # Only sell if meaningful contribution
                continue

            # Check minimum order size
            min_size = MIN_ORDER_SIZES.get(pair, 0)
            if min_size > 0 and sell_qty < min_size:
                continue

            print(f"   Selling {sell_qty:.6f} {base_curr} via {pair} (~${sell_value:.2f})...")
            success, result = place_market_order(subaccount_id, pair, "SELL", sell_qty, is_base_amount=True)
            if success:
                print(f"      ✅ Order {result[:24]} placed")
                remaining -= sell_value
            else:
                print(f"      ❌ Failed: {result}")
            time.sleep(0.3)

def buy_base_assets(balances, account_name, shortages, quote_results, subaccount_id):
    """Buy base assets that are running low."""
    if not shortages:
        return

    print(f"\n📦 {account_name} - Buying low base assets")

    # Group shortages by quote currency
    by_quote = {}
    for s in shortages:
        q = s['quote_currency']
        if q not in by_quote:
            by_quote[q] = []
        by_quote[q].append(s)

    for quote_curr, items in by_quote.items():
        quote_usd = quote_results.get(quote_curr, {}).get('usd_value', 0)
        usable = max(0, quote_usd - MIN_BALANCE_USD)

        if usable < 2:
            print(f"   ⏭️  Insufficient {quote_curr} (${quote_usd:.2f}) to buy base assets")
            continue

        # Sort by most critical (lowest cycles first)
        items.sort(key=lambda x: x['cycles'])

        remaining_usd = usable
        for shortage in items:
            if remaining_usd <= 0:
                break

            buy_value = shortage['shortfall'] * shortage['price_usd']
            if buy_value > remaining_usd:
                buy_value = remaining_usd

            if buy_value < 1:
                continue

            pair = shortage['pair']
            # Check minimum order size
            base_curr = get_base_currency(pair)
            min_size = MIN_ORDER_SIZES.get(pair, 0)
            if min_size > 0:
                # For buy with quoteAmount, check if quote amount meets minimum
                if buy_value < min_size * USD_PRICES.get(base_curr, 1):
                    # Try with a smaller min check - the min_size is base amount
                    # Convert min_size to USD
                    min_usd = min_size * USD_PRICES.get(base_curr, 1)
                    if buy_value < min_usd:
                        continue

            print(f"   Buying {base_curr} with ${buy_value:.2f} {quote_curr} via {pair}...")
            success, result = place_market_order(subaccount_id, pair, "BUY", buy_value, is_base_amount=False)
            if success:
                print(f"      ✅ Order {result[:24]} placed")
                remaining_usd -= buy_value
            else:
                print(f"      ❌ Failed: {result}")
            time.sleep(0.3)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("="*80)
    print("💰 Quote/Base Currency Replenisher")
    print("   Monitoring: USDT, USDC, ZAR")
    print("="*80)

    # Fetch market prices
    print("\n📊 Fetching live market prices...")
    market_prices = get_market_prices()
    if market_prices:
        print(f"   ✅ Got prices for {len(market_prices)} pairs")
        # Update ZAR rate if available
        if 'USDTZAR' in market_prices:
            global ZAR_USD_RATE
            ZAR_USD_RATE = 1.0 / market_prices['USDTZAR']
            print(f"   USDT/ZAR: {market_prices['USDTZAR']:.4f}")
    else:
        print("   ⚠️  Using fallback prices")

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

        # Step 1: Check all three quote currencies
        quote_results = check_quote_balances(balances, account_name, market_prices)

        # Step 2: Analyze base asset inventory
        shortages = analyze_base_inventory(balances, account_name, market_prices, quote_results)

        # Step 3: Convert between quote currencies if needed
        replenish_quote_currencies(balances, account_name, quote_results, subaccount_id)

        # Step 4: Sell surplus base assets for quote replenishment
        sell_base_for_quote(balances, account_name, quote_results, subaccount_id, market_prices)

        # Step 5: Buy low base assets
        buy_base_assets(balances, account_name, shortages, quote_results, subaccount_id)

        print(f"\n   ✅ {account_name} replenishment pass complete")

    print("\n" + "="*80)
    print("✅ Full replenishment pass complete")
    print("="*80)

if __name__ == "__main__":
    main()
