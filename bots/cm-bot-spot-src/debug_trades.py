#!/usr/bin/env python3
"""Debug: print trade structure"""

import hmac
import hashlib
import time
import requests
import json

MAIN_API_KEY = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
MAIN_API_SECRET = "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"
CM1_SUBACCOUNT_ID = "1483815480334401536"

BASE_URL = "https://api.valr.com"

def generate_signature(secret, timestamp_ms, method, path, body, subaccount_id):
    message = f"{timestamp_ms}{method}{path}{body}{subaccount_id}"
    signature = hmac.new(
        secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha512
    ).hexdigest()
    return signature

def fetch_trade_history(subaccount_id, limit=100):
    timestamp_ms = str(int(time.time() * 1000))
    method = "GET"
    path = "/v1/account/tradehistory"
    query_params = f"?limit={limit}"
    body = ""
    signature_path = path + query_params
    signature = generate_signature(MAIN_API_SECRET, timestamp_ms, method, signature_path, body, subaccount_id)
    
    headers = {
        "X-VALR-API-KEY": MAIN_API_KEY,
        "X-VALR-SIGNATURE": signature,
        "X-VALR-TIMESTAMP": timestamp_ms,
        "X-VALR-SUB-ACCOUNT-ID": subaccount_id
    }
    
    url = f"{BASE_URL}{path}{query_params}"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

trades = fetch_trade_history(CM1_SUBACCOUNT_ID, limit=10)
print("Sample trade structure:")
print(json.dumps(trades[0] if trades else {}, indent=2))
print(f"\nTotal trades: {len(trades)}")
if trades:
    print(f"\nKeys in first trade: {trades[0].keys()}")
