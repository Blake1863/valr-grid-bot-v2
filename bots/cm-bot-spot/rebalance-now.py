#!/usr/bin/env python3
"""Manual rebalance trigger for cm-bot-spot"""

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

def transfer(currency, amount, from_id, to_id, api_key, api_secret):
    path = "/v1/account/subaccounts/transfer"
    body = json.dumps({
        "fromId": from_id,
        "toId": to_id,
        "currencyCode": currency,
        "amount": f"{amount:.8f}",
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
    
    print(f"\n🏦 CMS1: {len(bal1)} assets")
    print(f"🏦 CMS2: {len(bal2)} assets")
    
    # Collect all unique assets
    all_assets = set(bal1.keys()) | set(bal2.keys())
    
    print(f"\n🔄 Checking {len(all_assets)} assets for rebalancing...\n")
    
    REBALANCE_THRESHOLD = 0.60
    
    for asset in sorted(all_assets):
        c1 = bal1.get(asset, 0.0)
        c2 = bal2.get(asset, 0.0)
        total = c1 + c2
        
        if total < 0.0001:
            continue
        
        c1_pct = c1 / total
        c2_pct = c2 / total
        
        imbalanced = c1_pct > REBALANCE_THRESHOLD or c2_pct > REBALANCE_THRESHOLD
        
        status = "⚠️ IMBALANCED" if imbalanced else "✅ OK"
        print(f"{asset}: CMS1={c1:.6f} ({c1_pct:.1%}) | CMS2={c2:.6f} ({c2_pct:.1%}) | Total={total:.6f} {status}")
        
        if imbalanced:
            # Determine transfer direction
            if c1 > c2:
                from_id, to_id, from_label, to_label = CMS1_ID, CMS2_ID, "CMS1", "CMS2"
                surplus = c1 - (total / 2)
            else:
                from_id, to_id, from_label, to_label = CMS2_ID, CMS1_ID, "CMS2", "CMS1"
                surplus = c2 - (total / 2)
            
            transfer_amt = surplus
            
            # Skip tiny transfers (< $1 USD equiv, assuming ~$1 min)
            if transfer_amt < 0.01:
                print(f"  └─ Skipping (too small)\n")
                continue
            
            print(f"  └─ Transferring {transfer_amt:.6f} from {from_label} to {to_label}...")
            status_code, resp_text = transfer(asset, transfer_amt, from_id, to_id, api_key, api_secret)
            if status_code in [200, 202]:
                print(f"  └─ ✅ Transfer initiated\n")
            else:
                print(f"  └─ ❌ Failed: {status_code} - {resp_text}\n")

if __name__ == "__main__":
    main()
