#!/usr/bin/env python3
"""Transfer all assets from CMS1/CMS2 to main, then liquidate to ZAR and USDT."""

import hmac
import hashlib
import time
import requests
import json

# From cm-bot-spot/.env
API_KEY = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
API_SECRET = "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"

CMS1_ID = "1483815480334401536"
CMS2_ID = "1483815498551132160"
MAIN_ID = "0"

def sign_request(verb, path, body="", subaccount=""):
    ts = str(int(time.time() * 1000))
    msg = ts + verb + path + body + subaccount
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()
    return ts, sig

def get_balances(subaccount_id=""):
    ts, sig = sign_request("GET", "/v1/account/balances", "", subaccount_id)
    headers = {
        "X-VALR-API-KEY": API_KEY,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts
    }
    if subaccount_id:
        headers["X-VALR-SUB-ACCOUNT-ID"] = subaccount_id
    
    resp = requests.get("https://api.valr.com/v1/account/balances", headers=headers)
    if resp.status_code != 200:
        print(f"  ERROR {resp.status_code}: {resp.text}")
        return {}
    data = resp.json()
    balances = {}
    for b in data:
        avail = float(b.get("available", 0))
        if avail > 0:
            balances[b["currency"]] = avail
    return balances

def transfer(from_id, to_id, currency, amount):
    body_dict = {
        "fromId": from_id,
        "toId": to_id,
        "currencyCode": currency,
        "amount": f"{amount:.8f}" if amount < 1 else f"{amount:.4f}",
        "allowBorrow": False
    }
    body = json.dumps(body_dict)
    ts, sig = sign_request("POST", "/v1/account/subaccounts/transfer", body, "")
    
    headers = {
        "X-VALR-API-KEY": API_KEY,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
        "Content-Type": "application/json"
    }
    
    resp = requests.post("https://api.valr.com/v1/account/subaccounts/transfer", headers=headers, json=body_dict)
    return resp.status_code, resp.text

def main():
    print("=== Current Balances ===\n")
    
    print("CMS1:")
    cms1 = get_balances(CMS1_ID)
    for curr, amt in sorted(cms1.items()):
        print(f"  {curr}: {amt}")
    
    print("\nCMS2:")
    cms2 = get_balances(CMS2_ID)
    for curr, amt in sorted(cms2.items()):
        print(f"  {curr}: {amt}")
    
    print("\n=== Transferring to Main (keeping ZAR) ===\n")
    
    # Transfer from CMS1
    print("CMS1 → Main:")
    for curr, amt in cms1.items():
        if curr == "ZAR":
            print(f"  ⏭️  {curr}: {amt} (keeping)")
            continue
        # Trim amount to avoid dust issues
        transfer_amt = amt * 0.9999 if amt > 0.001 else amt  # Leave tiny dust
        status, resp = transfer(CMS1_ID, MAIN_ID, curr, transfer_amt)
        icon = "✅" if status == 200 else "❌"
        print(f"  {icon} {curr}: {transfer_amt:.6f} → {status}")
        if status != 200:
            print(f"      Response: {resp[:100]}")
    
    # Transfer from CMS2
    print("\nCMS2 → Main:")
    for curr, amt in cms2.items():
        if curr == "ZAR":
            print(f"  ⏭️  {curr}: {amt} (keeping)")
            continue
        transfer_amt = amt * 0.9999 if amt > 0.001 else amt
        status, resp = transfer(CMS2_ID, MAIN_ID, curr, transfer_amt)
        icon = "✅" if status == 200 else "❌"
        print(f"  {icon} {curr}: {transfer_amt:.6f} → {status}")
        if status != 200:
            print(f"      Response: {resp[:100]}")
    
    print("\n=== Done ===")
    print("Check main account balances, then manually sell assets for ZAR/USDT on VALR")

if __name__ == "__main__":
    main()
