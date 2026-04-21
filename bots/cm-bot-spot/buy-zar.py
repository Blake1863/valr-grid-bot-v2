#!/usr/bin/env python3
"""
Buy R50 ZAR using USDT on CMS1 and CMS2 via limit order at market price.
"""

import subprocess
import requests
import hmac
import hashlib
import time
import json

API_BASE = "https://api.valr.com"

def load_env():
    env = {}
    with open("/home/admin/.openclaw/workspace/bots/cm-bot-spot/.env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                env[key.strip()] = val.strip()
    return env

def sign_request(method, path, body, subaccount, api_secret):
    ts = str(int(time.time() * 1000))
    sub_str = "" if subaccount == 0 else str(subaccount)
    msg = f"{ts}{method.upper()}{path}{body}{sub_str}"
    signature = hmac.new(
        api_secret.encode('utf-8'),
        msg.encode('utf-8'),
        hashlib.sha512
    ).hexdigest()
    return ts, signature

def get_balances(subaccount, api_key, api_secret):
    path = "/v1/account/balances"
    ts, sig = sign_request("GET", path, "", subaccount, api_secret)
    
    headers = {
        "X-VALR-API-KEY": api_key,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
        "X-VALR-SUB-ACCOUNT-ID": str(subaccount)
    }
    
    resp = requests.get(f"{API_BASE}{path}", headers=headers)
    if resp.status_code != 200:
        print(f"❌ Balance fetch failed: {resp.status_code} - {resp.text}")
        return None
    
    data = resp.json()
    return {b['currency']: float(b['available']) for b in data if b.get('available')}

def get_ticker(pair):
    resp = requests.get(f"{API_BASE}/v2/ticker?currencyPair={pair}")
    if resp.status_code == 200:
        data = resp.json()
        return float(data.get('lastPrice', 0))
    return None

def place_limit_order(subaccount, api_key, api_secret, pair, side, quantity, price):
    path = "/v1/orders/limit"
    body = json.dumps({
        "pair": pair,
        "side": side,
        "type": "LIMIT",
        "quantity": f"{quantity:.8f}",
        "price": f"{price:.4f}",
        "timeInForce": "IOC"
    })
    
    ts, sig = sign_request("POST", path, body, subaccount, api_secret)
    
    headers = {
        "X-VALR-API-KEY": api_key,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
        "X-VALR-SUB-ACCOUNT-ID": str(subaccount),
        "Content-Type": "application/json"
    }
    
    resp = requests.post(f"{API_BASE}{path}", headers=headers, data=body)
    return resp.status_code, resp.text

def main():
    print("🔑 Loading credentials from .env...")
    env = load_env()
    
    api_key = env.get('MAIN_API_KEY')
    api_secret = env.get('MAIN_API_SECRET')
    cms1_id = int(env.get('CM1_SUBACCOUNT_ID', 0))
    cms2_id = int(env.get('CM2_SUBACCOUNT_ID', 0))
    
    print(f"📊 Fetching balances and prices...")
    
    # Get USDT balances
    cms1_bal = get_balances(cms1_id, api_key, api_secret)
    cms2_bal = get_balances(cms2_id, api_key, api_secret)
    
    if not cms1_bal or not cms2_bal:
        print("❌ Failed to fetch balances")
        return
    
    cms1_usdt = cms1_bal.get('USDT', 0)
    cms2_usdt = cms2_bal.get('USDT', 0)
    
    print(f"🏦 CMS1: USDT={cms1_usdt:.2f}")
    print(f"🏦 CMS2: USDT={cms2_usdt:.2f}")
    
    # Use estimated USDTZAR price (~16.45 based on USDCZAR)
    price = 16.45
    print(f"💱 USDTZAR price (est): {price:.4f}")
    
    # Calculate USDT needed for R50
    zar_target = 50.0
    usdt_needed = zar_target / price
    
    print(f"🎯 Target: R{zar_target} ZAR per account")
    print(f"📉 Selling {usdt_needed:.4f} USDT per account")
    
    if cms1_usdt < usdt_needed:
        print(f"⚠️ CMS1 insufficient USDT: has {cms1_usdt:.4f}, needs {usdt_needed:.4f}")
        return
    if cms2_usdt < usdt_needed:
        print(f"⚠️ CMS2 insufficient USDT: has {cms2_usdt:.4f}, needs {usdt_needed:.4f}")
        return
    
    # Place sell orders (sell USDT for ZAR)
    print(f"\n📤 Placing SELL orders for USDTZAR...")
    
    # CMS1
    status1, resp1 = place_limit_order(cms1_id, api_key, api_secret, "USDTZAR", "SELL", usdt_needed, price * 0.999)
    if status1 == 200:
        print(f"✅ CMS1 order placed: {resp1[:100]}")
    else:
        print(f"❌ CMS1 order failed: {status1} - {resp1}")
    
    # CMS2
    status2, resp2 = place_limit_order(cms2_id, api_key, api_secret, "USDTZAR", "SELL", usdt_needed, price * 0.999)
    if status2 == 200:
        print(f"✅ CMS2 order placed: {resp2[:100]}")
    else:
        print(f"❌ CMS2 order failed: {status2} - {resp2}")
    
    print("\n⏳ Waiting 5s for fills...")
    time.sleep(5)
    
    # Check new balances
    print("\n📊 New balances:")
    cms1_bal_new = get_balances(cms1_id, api_key, api_secret)
    cms2_bal_new = get_balances(cms2_id, api_key, api_secret)
    
    if cms1_bal_new:
        print(f"🏦 CMS1: ZAR={cms1_bal_new.get('ZAR', 0):.2f} USDT={cms1_bal_new.get('USDT', 0):.2f}")
    if cms2_bal_new:
        print(f"🏦 CMS2: ZAR={cms2_bal_new.get('ZAR', 0):.2f} USDT={cms2_bal_new.get('USDT', 0):.2f}")

if __name__ == "__main__":
    main()
