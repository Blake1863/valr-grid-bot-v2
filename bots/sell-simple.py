#!/usr/bin/env python3
"""Simple sell script - sell assets to ZAR or USDC."""

import hmac
import hashlib
import time
import requests
import json
import uuid

API_KEY = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
API_SECRET = "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"

SKIP = ["USDT", "USDC", "ZAR"]

# Known working pairs on VALR
WORKING_PAIRS = {
    "BTC": ["ZAR", "USDT", "USDC"],
    "ETH": ["ZAR", "USDT", "USDC"],
    "SOL": ["ZAR", "USDT", "USDC"],
    "XRP": ["ZAR", "USDT", "USDC"],
    "AVAX": ["ZAR", "USDT", "USDC"],
    "BNB": ["ZAR", "USDT", "USDC"],
    "LINK": ["ZAR", "USDT", "USDC"],
    "DOGE": ["ZAR", "USDT"],
    "TRX": ["ZAR", "USDT"],
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
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()
    return ts, sig

def get_balances():
    ts, sig = sign("GET", "/v1/account/balances")
    headers = {"X-VALR-API-KEY": API_KEY, "X-VALR-SIGNATURE": sig, "X-VALR-TIMESTAMP": ts}
    resp = requests.get("https://api.valr.com/v1/account/balances", headers=headers)
    if resp.status_code != 200:
        print(f"Balance error: {resp.text}")
        return {}
    return {b["currency"]: float(b["available"]) for b in resp.json() if float(b["available"]) > 0}

def get_bid(pair):
    """Get best bid, return 0 if no liquidity"""
    for attempt in range(3):
        try:
            resp = requests.get(f"https://api.valr.com/v1/public/{pair}/orderbook", timeout=5)
            if resp.status_code == 200:
                d = resp.json()
                bids = d.get("Bids", d.get("bids", []))
                if bids:
                    return float(bids[0]["price"])
            time.sleep(1)  # Rate limit
        except:
            time.sleep(1)
    return 0

def sell(pair, qty, price):
    """Place IOC sell order"""
    cid = str(uuid.uuid4())
    body = {
        "currencyPair": pair, "side": "SELL", "type": "LIMIT",
        "quantity": f"{qty:.8f}" if qty < 1 else f"{qty:.4f}",
        "price": f"{price:.2f}" if "ZAR" in pair else f"{price:.8f}",
        "timeInForce": "IOC", "customerOrderId": cid
    }
    ts, sig = sign("POST", "/v1/orders", json.dumps(body))
    headers = {
        "X-VALR-API-KEY": API_KEY, "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts, "Content-Type": "application/json"
    }
    resp = requests.post("https://api.valr.com/v1/orders", headers=headers, json=body)
    return resp.status_code, resp.text[:100]

def main():
    print("=== Liquidating Assets ===\n")
    balances = get_balances()
    
    sold = []
    failed = []
    
    for curr, amt in sorted(balances.items()):
        if curr in SKIP:
            print(f"  ⏭️  {curr}: {amt:.4f} (skipped)")
            continue
        
        quotes = WORKING_PAIRS.get(curr, ["ZAR", "USDC"])
        pair = None
        price = 0
        
        for q in quotes:
            p = f"{curr}{q}"
            pr = get_bid(p)
            if pr > 0:
                pair = p
                price = pr
                break
        
        if not pair:
            failed.append((curr, amt, "No liquid pair"))
            print(f"  ❌ {curr:12} {amt:>15.6f} | No orderbook")
            continue
        
        status, resp = sell(pair, amt, price)
        if status in [200, 202]:
            sold.append((curr, amt, pair, price, amt*price))
            print(f"  ✅ {curr:12} {amt:>15.6f} | {pair:15} @ {price:.4f}")
        else:
            failed.append((curr, amt, f"{status}: {resp}"))
            print(f"  ❌ {curr:12} {amt:>15.6f} | {status}")
        time.sleep(0.5)
    
    print(f"\n=== Done: {len(sold)} sold, {len(failed)} failed ===")
    if failed:
        print("Failed:", [f[0] for f in failed])

if __name__ == "__main__":
    main()
