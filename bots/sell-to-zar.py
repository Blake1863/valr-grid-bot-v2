#!/usr/bin/env python3
"""Sell all assets for ZAR (primary) or USDC (fallback)."""

import hmac
import hashlib
import time
import requests
import json
import uuid

API_KEY = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
API_SECRET = "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"

# Primary: ZAR pairs. Fallback: USDC pairs
PAIR_MAP = {
    "BTC": "ZAR", "ETH": "ZAR", "SOL": "ZAR", "AVAX": "ZAR",
    "XRP": "ZAR", "LINK": "ZAR", "DOGE": "ZAR", "TRX": "ZAR",
    "BNB": "ZAR", "WIF": "USDC", "JUP": "USDC", "PYTH": "USDC",
    "SHIB": "USDC", "BOME": "USDC", "SWEAT": "USDC",
    "MSTRX": "USDC", "HOODX": "USDC", "SPYX": "USDC",
    "TRUMP": "USDC", "BITGOLD": "USDC", "XAUT": "ZAR",
    "ZRO": "USDC", "EURC": "USDC",
}

SKIP = ["USDT", "USDC", "ZAR"]

def sign_request(verb, path, body=""):
    ts = str(int(time.time() * 1000))
    msg = ts + verb + path + body + ""
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()
    return ts, sig

def get_balances():
    ts, sig = sign_request("GET", "/v1/account/balances")
    headers = {"X-VALR-API-KEY": API_KEY, "X-VALR-SIGNATURE": sig, "X-VALR-TIMESTAMP": ts}
    resp = requests.get("https://api.valr.com/v1/account/balances", headers=headers)
    if resp.status_code != 200:
        return {}
    data = resp.json()
    return {b["currency"]: float(b["available"]) for b in data if float(b["available"]) > 0}

def get_best_bid(pair):
    """Get best bid price from orderbook"""
    try:
        resp = requests.get(f"https://api.valr.com/v1/public/{pair}/orderbook", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            bids = data.get("Bids", [])  # VALR uses capital B
            if bids:
                return float(bids[0]["price"])
    except Exception as e:
        pass
    return 0

def place_sell_order(pair, quantity, price):
    """Place IOC sell order"""
    customer_order_id = str(uuid.uuid4())
    # Format quantity and price based on pair
    if "ZAR" in pair:
        qty_str = f"{quantity:.8f}" if quantity < 1 else f"{quantity:.4f}"
        price_str = f"{price:.2f}"  # ZAR pairs use 2dp
    else:
        qty_str = f"{quantity:.8f}" if quantity < 1 else f"{quantity:.4f}"
        price_str = f"{price:.8f}" if price < 1 else f"{price:.2f}"
    
    body_dict = {
        "currencyPair": pair,
        "side": "SELL",
        "type": "LIMIT",
        "quantity": qty_str,
        "price": price_str,
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
    print("=== Selling All Assets (ZAR primary, USDC fallback) ===\n")
    
    balances = get_balances()
    if not balances:
        print("No balances found")
        return
    
    print(f"Found {len(balances)} assets\n")
    
    results = {"sold": [], "failed": [], "skipped": []}
    total_zar = 0
    total_usdc = 0
    
    for curr, amt in sorted(balances.items()):
        if curr in SKIP:
            results["skipped"].append((curr, amt))
            continue
        
        quote = PAIR_MAP.get(curr, "ZAR")  # Default to ZAR
        pair = f"{curr}{quote}"
        
        # Get price
        time.sleep(0.5)  # Rate limit
        price = get_best_bid(pair)
        
        if price <= 0:
            # Try alternate quote
            alt_quote = "USDC" if quote == "ZAR" else "ZAR"
            alt_pair = f"{curr}{alt_quote}"
            time.sleep(0.5)
            price = get_best_bid(alt_pair)
            if price > 0:
                pair = alt_pair
                quote = alt_quote
            else:
                results["failed"].append((curr, amt, f"No liquidity in {curr}ZAR or {curr}USDC"))
                print(f"  ❌ {curr:12} {amt:>15.6f} | No orderbook")
                continue
        
        est_value = amt * price
        
        # Place IOC sell
        time.sleep(0.3)
        status, resp = place_sell_order(pair, amt, price)
        
        if status == 202 or status == 200:
            results["sold"].append((curr, amt, pair, price, est_value))
            if quote == "ZAR":
                total_zar += est_value
            else:
                total_usdc += est_value
            print(f"  ✅ {curr:12} {amt:>15.6f} | {pair:15} @ {price:.4f} ≈ {quote} {est_value:.2f}")
        else:
            results["failed"].append((curr, amt, pair, f"Error {status}"))
            print(f"  ❌ {curr:12} {amt:>15.6f} | {pair:15} - {status}")
    
    print(f"\n=== Summary ===")
    print(f"  Sold: {len(results['sold'])}")
    print(f"  Failed: {len(results['failed'])}")
    print(f"  Skipped: {len(results['skipped'])}")
    print(f"\n  Estimated: ZAR {total_zar:.2f} + USDC {total_usdc:.2f}")
    
    if results["failed"]:
        print("\nFailed:")
        for item in results["failed"]:
            print(f"  - {item[0]}: {item[3]}")

if __name__ == "__main__":
    main()
