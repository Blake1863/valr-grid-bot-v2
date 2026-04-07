#!/usr/bin/env python3
"""Liquidate all assets - with proper rate limiting."""

import hmac
import hashlib
import time
import requests
import json
import uuid

API_KEY = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
API_SECRET = "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"

SKIP = ["USDT", "USDC", "ZAR"]

# Pairs to try for each asset (in priority order)
PAIRS = {
    "BTC": ["ZAR", "USDT", "USDC"],
    "ETH": ["ZAR", "USDT", "USDC"],
    "SOL": ["ZAR", "USDT", "USDC"],
    "XRP": ["ZAR", "USDT", "USDC"],
    "AVAX": ["ZAR", "USDT", "USDC"],
    "BNB": ["ZAR", "USDT", "USDC"],
    "LINK": ["ZAR", "USDT", "USDC"],
    "DOGE": ["ZAR", "USDT", "USDC"],
    "TRX": ["ZAR", "USDT", "USDC"],
    "EURC": ["USDC", "ZAR"],
    "XAUT": ["ZAR", "USDT"],
    "SHIB": ["USDC", "USDT"],
    "WIF": ["USDC", "USDT"],
    "JUP": ["USDC"],
    "PYTH": ["USDC"],
    "BOME": ["USDC"],
    "SWEAT": ["USDC"],
    "MSTRX": ["USDC"],
    "HOODX": ["USDC"],
    "SPYX": ["USDC"],
    "TRUMP": ["USDC"],
    "BITGOLD": ["USDC"],
    "ZRO": ["USDC"],
}

def sign(verb, path, body=""):
    ts = str(int(time.time() * 1000))
    msg = ts + verb + path + body + ""
    return ts, hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()

def get_balances():
    ts, sig = sign("GET", "/v1/account/balances")
    r = requests.get("https://api.valr.com/v1/account/balances", 
        headers={"X-VALR-API-KEY": API_KEY, "X-VALR-SIGNATURE": sig, "X-VALR-TIMESTAMP": ts})
    return {b["currency"]: float(b["available"]) for b in r.json()} if r.status_code == 200 else {}

def get_bid(pair):
    r = requests.get(f"https://api.valr.com/v1/public/{pair}/orderbook", timeout=5)
    if r.status_code == 200:
        d = r.json()
        bids = d.get("Bids", [])
        if bids:
            return float(bids[0]["price"])
    return 0

def sell(pair, qty, price):
    cid = str(uuid.uuid4())
    body = {
        "currencyPair": pair, "side": "SELL", "type": "LIMIT",
        "quantity": f"{qty:.8f}" if qty < 1 else f"{qty:.4f}",
        "price": f"{price:.2f}" if "ZAR" in pair else f"{price:.6f}",
        "timeInForce": "IOC", "customerOrderId": cid
    }
    ts, sig = sign("POST", "/v1/orders", json.dumps(body))
    r = requests.post("https://api.valr.com/v1/orders", 
        headers={"X-VALR-API-KEY": API_KEY, "X-VALR-SIGNATURE": sig, "X-VALR-TIMESTAMP": ts, "Content-Type": "application/json"},
        json=body)
    return r.status_code, r.text[:80]

def main():
    print("=== Liquidating All Assets ===\n")
    balances = get_balances()
    
    total_zar = 0
    total_usdc = 0
    sold = []
    
    for curr, amt in sorted(balances.items(), key=lambda x: -x[1]):
        if curr in SKIP:
            print(f"  ⏭️  {curr}: {amt:.4f}")
            continue
        if amt < 0.0001:
            print(f"  ⏭️  {curr}: {amt:.6f} (dust)")
            continue
        
        # Find working pair
        pair = None
        price = 0
        for q in PAIRS.get(curr, ["ZAR"]):
            p = f"{curr}{q}"
            pr = get_bid(p)
            if pr > 0:
                pair, price = p, pr
                break
            time.sleep(0.5)
        
        if not pair:
            print(f"  ❌ {curr:12} {amt:>15.6f} | No liquidity")
            time.sleep(0.5)
            continue
        
        # Sell
        status, resp = sell(pair, amt, price)
        val = amt * price
        if status in [200, 202]:
            sold.append((curr, amt, pair, price, val))
            if "ZAR" in pair:
                total_zar += val
            else:
                total_usdc += val
            print(f"  ✅ {curr:12} {amt:>15.6f} | {pair:15} @ {price:.4f} = {val:.2f}")
        else:
            print(f"  ❌ {curr:12} {amt:>15.6f} | {status}: {resp}")
        
        time.sleep(2)  # Rate limit
    
    print(f"\n=== Summary ===")
    print(f"  Sold: {len(sold)} assets")
    print(f"  Total: ≈ ZAR {total_zar:.2f} + USDC {total_usdc:.2f}")
    print(f"\n  Check main account for USDT/ZAR/USDC balances")

if __name__ == "__main__":
    main()
