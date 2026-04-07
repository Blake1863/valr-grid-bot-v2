#!/usr/bin/env python3
"""
Sell USDPC inventory on CMS1 and CMS2 via limit order at market price,
then transfer USDT to main account.
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
        return {}
    
    balances = resp.json()
    return {b["currency"]: float(b["available"]) for b in balances if float(b["available"]) > 0}

def get_orderbook(pair):
    """Get best bid price for market sell."""
    resp = requests.get(f"{API_BASE}/v1/public/{pair}/orderbook", timeout=10)
    if resp.status_code == 200:
        data = resp.json()
        if data.get("Bids"):
            return float(data["Bids"][0]["price"])
    return None

def place_limit_sell(subaccount, pair, qty, price, api_key, api_secret):
    """Place a limit SELL order (IOC to act like market)."""
    path = "/v1/orders/limit"
    body = json.dumps({
        "pair": pair,  # VALR uses 'pair' not 'currencyPair'
        "side": "SELL",
        "type": "LIMIT",
        "quantity": f"{qty:.8f}",
        "price": f"{price:.8f}",
        "timeInForce": "IOC"  # Immediate-or-cancel (acts like market)
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

def transfer_to_main(currency, amount, from_subaccount, api_key, api_secret):
    """Transfer from subaccount to main account."""
    path = "/v1/account/subaccounts/transfer"
    body = json.dumps({
        "fromId": from_subaccount,
        "toId": 0,  # Main account
        "currencyCode": currency,
        "amount": f"{amount:.2f}",  # VALR wants short decimal
        "allowBorrow": False
    })
    ts, sig = sign_request("POST", path, body, 0, api_secret)
    
    headers = {
        "X-VALR-API-KEY": api_key,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
        "Content-Type": "application/json"
    }
    
    resp = requests.post(f"{API_BASE}{path}", headers=headers, data=body)
    return resp.status_code, resp.text

def main():
    print("🔑 Loading credentials from .env...")
    env = load_env()
    api_key = env.get("MAIN_API_KEY")
    api_secret = env.get("MAIN_API_SECRET")
    CMS1_ID = env.get("CM1_SUBACCOUNT_ID", "1483815480334401536")
    CMS2_ID = env.get("CM2_SUBACCOUNT_ID", "1483815498551132160")
    
    if not all([api_key, api_secret]):
        print("❌ Missing credentials in .env")
        return
    
    print("📊 Fetching balances...")
    bal1 = get_balances(CMS1_ID, api_key, api_secret)
    bal2 = get_balances(CMS2_ID, api_key, api_secret)
    
    usdpc1 = bal1.get("USDPC", 0.0)
    usdpc2 = bal2.get("USDPC", 0.0)
    
    print(f"\n💰 USDPC Holdings:")
    print(f"   CMS1: {usdpc1:.4f}")
    print(f"   CMS2: {usdpc2:.4f}")
    print(f"   Total: {usdpc1 + usdpc2:.4f}")
    
    if usdpc1 < 0.01 and usdpc2 < 0.01:
        print("\n⚠️  No significant USDPC to sell")
        return
    
    # Get market price
    print("\n📈 Fetching USDPCUSDT orderbook...")
    price = get_orderbook("USDPCUSDT")
    if not price:
        print("  ❌ Could not get orderbook, using $1.13")
        price = 1.13
    print(f"   Best bid: ${price:.4f}")
    
    # Sell USDPC on both accounts
    print("\n🔨 Selling USDPC (IOC limit orders)...")
    
    if usdpc1 >= 0.01:
        print(f"   CMS1: Selling {usdpc1:.4f} USDPC @ ${price:.4f}...")
        status, resp = place_limit_sell(CMS1_ID, "USDPCUSDT", usdpc1, price, api_key, api_secret)
        if status in [200, 202]:
            print(f"   ✅ CMS1 sell order placed")
        else:
            print(f"   ❌ CMS1 sell failed: {status} - {resp[:100]}")
        time.sleep(0.5)
    
    if usdpc2 >= 0.01:
        print(f"   CMS2: Selling {usdpc2:.4f} USDPC @ ${price:.4f}...")
        status, resp = place_limit_sell(CMS2_ID, "USDPCUSDT", usdpc2, price, api_key, api_secret)
        if status in [200, 202]:
            print(f"   ✅ CMS2 sell order placed")
        else:
            print(f"   ❌ CMS2 sell failed: {status} - {resp[:100]}")
        time.sleep(0.5)
    
    # Wait for fills
    print("\n⏳ Waiting 5s for orders to fill...")
    time.sleep(5)
    
    # Fetch USDT balances
    print("\n📊 Fetching updated USDT balances...")
    bal1 = get_balances(CMS1_ID, api_key, api_secret)
    bal2 = get_balances(CMS2_ID, api_key, api_secret)
    
    usdt1 = bal1.get("USDT", 0.0)
    usdt2 = bal2.get("USDT", 0.0)
    
    print(f"   CMS1: ${usdt1:.2f} USDT")
    print(f"   CMS2: ${usdt2:.2f} USDT")
    
    # Transfer USDT to main
    print("\n💸 Transferring USDT to main account...")
    
    if usdt1 >= 1.0:
        print(f"   CMS1 → Main: ${usdt1:.2f} USDT...")
        status, resp = transfer_to_main("USDT", usdt1, CMS1_ID, api_key, api_secret)
        if status in [200, 202]:
            print(f"   ✅ Transfer initiated")
        else:
            print(f"   ❌ Transfer failed: {status} - {resp[:100]}")
        time.sleep(0.5)
    
    if usdt2 >= 1.0:
        print(f"   CMS2 → Main: ${usdt2:.2f} USDT...")
        status, resp = transfer_to_main("USDT", usdt2, CMS2_ID, api_key, api_secret)
        if status in [200, 202]:
            print(f"   ✅ Transfer initiated")
        else:
            print(f"   ❌ Transfer failed: {status} - {resp[:100]}")
        time.sleep(0.5)
    
    print("\n✅ Done!")

if __name__ == "__main__":
    main()
