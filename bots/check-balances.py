#!/usr/bin/env python3
"""Check balances and transfer all assets from CMS1/CMS2 to main account."""

import subprocess
import hmac
import hashlib
import time
import requests
import json

CMS1_ID = "1483815480334401536"
CMS2_ID = "1483815498551132160"
MAIN_ID = "0"

def get_secret(name):
    result = subprocess.run(
        ["python3", "/home/admin/.openclaw/secrets/secrets.py", "get", name],
        capture_output=True, text=True
    )
    return result.stdout.strip()

API_KEY = get_secret("valr_api_key")
API_SECRET = get_secret("valr_api_secret")

def sign_request(verb, path, body="", subaccount=""):
    ts = str(int(time.time() * 1000))
    msg = ts + verb + path + body + subaccount
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()
    return ts, sig

def get_balances(subaccount_id):
    ts, sig = sign_request("GET", "/v1/account/balances", "", subaccount_id)
    headers = {
        "X-VALR-API-KEY": API_KEY,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
        "X-VALR-SUB-ACCOUNT-ID": subaccount_id
    }
    resp = requests.get("https://api.valr.com/v1/account/balances", headers=headers)
    if resp.status_code != 200:
        print(f"ERROR {resp.status_code}: {resp.text}")
        return None
    data = resp.json()
    balances = data.get("balances", [])
    # Filter non-zero
    return {b["currency"]: float(b["availableInReference"]) for b in balances if float(b["availableInReference"]) > 0}

def transfer(from_id, to_id, currency, amount):
    ts, sig = sign_request("POST", "/v1/account/subaccounts/transfer", 
                           json.dumps({"fromId": from_id, "toId": to_id, "currencyCode": currency, "amount": str(amount), "allowBorrow": False}))
    headers = {
        "X-VALR-API-KEY": API_KEY,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
        "Content-Type": "application/json"
    }
    body = {"fromId": from_id, "toId": to_id, "currencyCode": currency, "amount": str(amount), "allowBorrow": False}
    resp = requests.post("https://api.valr.com/v1/account/subaccounts/transfer", headers=headers, json=body)
    return resp.status_code, resp.text

def main():
    print("=== CMS1 Balances ===")
    cms1_balances = get_balances(CMS1_ID)
    if cms1_balances:
        for curr, amt in cms1_balances.items():
            print(f"  {curr}: {amt}")
    else:
        print("  (none or error)")
    
    print("\n=== CMS2 Balances ===")
    cms2_balances = get_balances(CMS2_ID)
    if cms2_balances:
        for curr, amt in cms2_balances.items():
            print(f"  {curr}: {amt}")
    else:
        print("  (none or error)")
    
    print("\n=== Transferring CMS1 → Main ===")
    if cms1_balances:
        for curr, amt in cms1_balances.items():
            # Skip ZAR - we want to keep it
            if curr == "ZAR":
                print(f"  Skipping {curr} (keeping in subaccount)")
                continue
            status, resp = transfer(CMS1_ID, MAIN_ID, curr, amt)
            print(f"  {curr} {amt}: {status} - {resp}")
    
    print("\n=== Transferring CMS2 → Main ===")
    if cms2_balances:
        for curr, amt in cms2_balances.items():
            if curr == "ZAR":
                print(f"  Skipping {curr} (keeping in subaccount)")
                continue
            status, resp = transfer(CMS2_ID, MAIN_ID, curr, amt)
            print(f"  {curr} {amt}: {status} - {resp}")

if __name__ == "__main__":
    main()
