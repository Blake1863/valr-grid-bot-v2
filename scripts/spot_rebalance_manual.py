#!/usr/bin/env python3
"""
Manual spot account rebalancer for CMS1/CMS2.
Checks all enabled pairs and transfers assets to achieve 50/50 split.
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

# Enabled pairs from config
ENABLED_PAIRS = [
    "EURCUSDC", "JUPUSDT", "TRUMPUSDT", "SPYXUSDT", "VALR10USDT", 
    "BITGOLDUSDT", "MSTRXUSDT", "TSLAXUSDT", "HOODXUSDT", "CRCLXUSDT", 
    "COINXUSDT", "NVDAXUSDT", "USDPCUSDT"
]

REBALANCE_THRESHOLD = 0.60  # Rebalance if one account has >60%
MIN_TRANSFER_USD = 1.0

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

def get_available_balance(balances, currency):
    """Get available (not reserved) balance for a currency."""
    for bal in balances:
        if bal['currency'] == currency:
            return float(bal.get('available', 0))
    return 0.0

def transfer(currency, amount, from_id, to_id, dp=2):
    path = "/v1/account/subaccounts/transfer"
    body_dict = {
        "fromId": from_id,
        "toId": to_id,
        "currencyCode": currency,
        "amount": f"{amount:.{dp}f}",
        "allowBorrow": False
    }
    body = json.dumps(body_dict)
    timestamp, signature = sign_request("POST", path, body, "")
    
    headers = {
        "X-VALR-API-KEY": API_KEY,
        "X-VALR-SIGNATURE": signature,
        "X-VALR-TIMESTAMP": timestamp,
        "Content-Type": "application/json",
    }
    
    req = urllib.request.Request("https://api.valr.com" + path, data=body.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return True, response.read().decode()
    except urllib.error.HTTPError as e:
        return False, e.read().decode()

def get_assets_from_pairs():
    """Extract all unique assets from enabled pairs."""
    assets = set()
    for pair in ENABLED_PAIRS:
        # Split pair into base and quote (e.g., "EURCUSDC" -> "EURC", "USDC")
        # Most pairs end with USDT, USDC, or ZAR
        if pair.endswith("USDT"):
            base = pair[:-4]
            quote = "USDT"
        elif pair.endswith("USDC"):
            base = pair[:-4]
            quote = "USDC"
        elif pair.endswith("ZAR"):
            base = pair[:-3]
            quote = "ZAR"
        else:
            # Fallback - try to split in middle
            mid = len(pair) // 2
            base, quote = pair[:mid], pair[mid:]
        
        assets.add(base)
        assets.add(quote)
    return sorted(assets)

def main():
    print("="*70)
    print("🔄 CMS1/CMS2 Spot Account Rebalancer")
    print("="*70)
    
    # Fetch balances
    print("\n📊 Fetching balances...")
    cms1_balances = get_balances(CMS1_ID)
    cms2_balances = get_balances(CMS2_ID)
    
    # Convert to dict by currency - use AVAILABLE balance (not reserved)
    cms1 = {b['currency']: get_available_balance(cms1_balances, b['currency']) for b in cms1_balances}
    cms2 = {b['currency']: get_available_balance(cms2_balances, b['currency']) for b in cms2_balances}
    
    # Get all assets to check
    assets = get_assets_from_pairs()
    print(f"   Checking {len(assets)} assets: {', '.join(assets)}")
    
    # Approximate USD prices (simplified)
    usd_prices = {
        'USDT': 1.0, 'USDC': 1.0, 'USDPC': 1.0, 'EURC': 1.08,
        'ZAR': 0.055, 'JUP': 0.15, 'TRUMP': 10.0, 'SPYX': 600.0,
        'VALR10': 50.0, 'BITGOLD': 2000.0, 'MSTRX': 130.0,
        'TSLAX': 350.0, 'HOODX': 70.0, 'CRCLX': 100.0,
        'COINX': 250.0, 'NVDAX': 120.0
    }
    
    transfers_needed = []
    
    print("\n" + "="*70)
    print("Asset Analysis")
    print("="*70)
    
    for asset in assets:
        c1 = cms1.get(asset, 0)
        c2 = cms2.get(asset, 0)
        total = c1 + c2
        
        if total < 0.0001:
            continue  # No holdings
        
        c1_pct = c1 / total if total > 0 else 0
        c2_pct = c2 / total if total > 0 else 0
        
        imbalanced = c1_pct > REBALANCE_THRESHOLD or c2_pct > REBALANCE_THRESHOLD
        
        status = "⚠️ " if imbalanced else "✅"
        print(f"\n{status} {asset}:")
        print(f"   CMS1: {c1:>12.6f} ({c1_pct*100:>5.1f}%)")
        print(f"   CMS2: {c2:>12.6f} ({c2_pct*100:>5.1f}%)")
        print(f"   Total: {total:>11.6f}")
        
        if imbalanced:
            target = total / 2
            if c1 > c2:
                from_id, to_id = CMS1_ID, CMS2_ID
                from_label, to_label = "CMS1", "CMS2"
                surplus = c1 - target
            else:
                from_id, to_id = CMS2_ID, CMS1_ID
                from_label, to_label = "CMS2", "CMS1"
                surplus = c2 - target
            
            # Determine decimal places
            dp = 2 if asset in ['USDT', 'USDC', 'USDPC', 'EURC', 'ZAR'] else 6
            transfer_amt = round(surplus, dp)
            
            # Check minimum value
            price = usd_prices.get(asset, 1.0)
            if transfer_amt * price < MIN_TRANSFER_USD:
                print(f"   ⏭️  Skipping - transfer value ${transfer_amt * price:.2f} < ${MIN_TRANSFER_USD}")
                continue
            
            print(f"   → Transfer {transfer_amt:.{dp}f} {asset} from {from_label} to {to_label}")
            transfers_needed.append((asset, transfer_amt, from_id, to_id, from_label, to_label, dp))
    
    if not transfers_needed:
        print("\n✅ No rebalancing needed - all assets are balanced!")
        return
    
    print("\n" + "="*70)
    print(f"Executing {len(transfers_needed)} transfers...")
    print("="*70)
    
    for asset, amt, from_id, to_id, from_label, to_label, dp in transfers_needed:
        print(f"\n📤 Transferring {amt:.{dp}f} {asset} from {from_label} → {to_label}...")
        success, result = transfer(asset, amt, from_id, to_id, dp)
        if success:
            print(f"   ✅ Success!")
        else:
            print(f"   ❌ Failed: {result[:200]}")
        time.sleep(0.5)  # Rate limit
    
    print("\n" + "="*70)
    print("Rebalance complete!")
    print("="*70)

if __name__ == "__main__":
    main()
