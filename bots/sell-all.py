#!/usr/bin/env python3
"""Sell all assets for USDT (or USDC if USDT pair doesn't exist)."""

import hmac
import hashlib
import time
import requests
import json
import uuid

API_KEY = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
API_SECRET = "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"

# Map currencies to their quote currency (USDT preferred, USDC fallback)
PAIR_MAP = {
    "BTC": "USDT", "ETH": "USDT", "SOL": "USDT", "AVAX": "USDT",
    "XRP": "USDT", "LINK": "USDT", "DOGE": "USDT", "TRX": "USDT",
    "BNB": "USDT", "WIF": "USDT", "JUP": "USDT", "PYTH": "USDT",
    "SHIB": "USDT", "BOME": "USDT", "SWEAT": "USDT",
    "MSTRX": "USDT", "HOODX": "USDT", "SPYX": "USDT",
    "TRUMP": "USDT", "BITGOLD": "USDT", "XAUT": "USDT",
    "ZRO": "USDT", "EURC": "USDC",  # EURC trades vs USDC
}

# Assets to skip (already stable or fiat)
SKIP = ["USDT", "USDC", "ZAR"]

def sign_request(verb, path, body=""):
    ts = str(int(time.time() * 1000))
    msg = ts + verb + path + body + ""
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()
    return ts, sig

def get_balances():
    ts, sig = sign_request("GET", "/v1/account/balances")
    headers = {
        "X-VALR-API-KEY": API_KEY,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts
    }
    resp = requests.get("https://api.valr.com/v1/account/balances", headers=headers)
    if resp.status_code != 200:
        print(f"Balance error: {resp.text}")
        return {}
    data = resp.json()
    return {b["currency"]: float(b["available"]) for b in data if float(b["available"]) > 0}

def get_orderbook(pair):
    """Get best bid price"""
    try:
        resp = requests.get(f"https://api.valr.com/v1/public/{pair}/orderbook", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            bids = data.get("bids", [])
            if bids:
                return float(bids[0]["price"])
    except:
        pass
    return 0

def place_sell_order(pair, quantity, price):
    """Place IOC sell order"""
    customer_order_id = str(uuid.uuid4())
    body_dict = {
        "currencyPair": pair,
        "side": "SELL",
        "type": "LIMIT",
        "quantity": f"{quantity:.8f}" if quantity < 1 else f"{quantity:.4f}",
        "price": f"{price:.2f}" if price >= 1 else f"{price:.8f}",
        "timeInForce": "IOC",
        "customerOrderId": customer_order_id
    }
    body = json.dumps(body_dict)
    ts, sig = sign_request("POST", "/v1/orders", body)
    
    headers = {
        "X-VALR-API-KEY": API_KEY,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
        "Content-Type": "application/json"
    }
    
    resp = requests.post("https://api.valr.com/v1/orders", headers=headers, json=body_dict)
    return resp.status_code, resp.text

def main():
    print("=== Selling All Assets for USDT/USDC ===\n")
    
    balances = get_balances()
    if not balances:
        print("No balances found or API error")
        return
    
    print(f"Found {len(balances)} assets\n")
    
    results = {"sold": [], "failed": [], "skipped": []}
    
    for curr, amt in sorted(balances.items()):
        if curr in SKIP:
            results["skipped"].append((curr, amt, "Already stable/fiat"))
            continue
        
        quote = PAIR_MAP.get(curr, "USDT")
        pair = f"{curr}{quote}"
        
        # Get price
        time.sleep(0.5)  # Rate limit
        price = get_orderbook(pair)
        
        if price <= 0:
            results["failed"].append((curr, amt, pair, "No liquidity"))
            print(f"  ❌ {curr:12} {amt:>15.6f} | {pair:15} - No orderbook")
            continue
        
        est_value = amt * price
        
        # Place IOC sell
        time.sleep(0.3)
        status, resp = place_sell_order(pair, amt, price)
        
        if status == 202 or status == 200:
            results["sold"].append((curr, amt, pair, price, est_value))
            print(f"  ✅ {curr:12} {amt:>15.6f} | {pair:15} @ ${price:.6f} ≈ ${est_value:.2f}")
        else:
            results["failed"].append((curr, amt, pair, f"Error {status}: {resp[:50]}"))
            print(f"  ❌ {curr:12} {amt:>15.6f} | {pair:15} - {status}: {resp[:60]}")
    
    print("\n=== Summary ===")
    print(f"  Sold: {len(results['sold'])}")
    print(f"  Failed: {len(results['failed'])}")
    print(f"  Skipped: {len(results['skipped'])}")
    
    if results["failed"]:
        print("\nFailed assets:")
        for item in results["failed"]:
            print(f"  - {item[0]}: {item[3]}")

if __name__ == "__main__":
    main()
