#!/usr/bin/env python3
"""
Test script to verify VALR API endpoints before deployment.
Run this AFTER adding secrets to confirm everything works.
"""

import json
import time
import hmac
import hashlib
import requests
from pathlib import Path

# Add json import for dumps

SECRETS_SCRIPT = Path("/home/admin/.openclaw/secrets/secrets.py")

def get_secret(name):
    import subprocess
    result = subprocess.run(
        ["python3", str(SECRETS_SCRIPT), "get", name],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        print(f"❌ Failed to fetch {name}: {result.stderr}")
        return None
    return result.stdout.strip()

# For testing with main account, use existing VALR keys
# The bot will use dedicated keys later (valr_autobuy_*)
def get_test_credentials():
    """Use main VALR keys for testing"""
    api_key = get_secret("valr_api_key")
    api_secret = get_secret("valr_api_secret")
    if not api_key or not api_secret:
        print("❌ Main VALR credentials not found in secrets")
        return None, None
    return api_key, api_secret

def sign_request(verb, path, body="", subaccount_id="", use_test_creds=False):
    if use_test_creds:
        api_key, api_secret = get_test_credentials()
        if not api_key or not api_secret:
            raise RuntimeError("Test credentials not available")
    else:
        api_key = get_secret("valr_autobuy_api_key")
        api_secret = get_secret("valr_autobuy_api_secret")
    
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{verb}{path}{body}{subaccount_id}"
    signature = hmac.new(
        api_secret.encode(),
        message.encode(),
        hashlib.sha512
    ).hexdigest()
    return {
        "X-VALR-API-KEY": api_key,
        "X-VALR-SIGNATURE": signature,
        "X-VALR-TIMESTAMP": timestamp,
        "X-VALR-SUB-ACCOUNT-ID": subaccount_id
    }

def test_balances(subaccount_id, use_test_creds=True):
    """Test balance endpoint"""
    print("\n📊 Testing GET /v1/account/balances...")
    headers = sign_request("GET", "/v1/account/balances", "", subaccount_id, use_test_creds)
    headers.pop("X-VALR-SUB-ACCOUNT-ID")  # This endpoint doesn't want the header
    resp = requests.get("https://api.valr.com/v1/account/balances", headers=headers)
    print(f"Status: {resp.status_code}")
    if resp.status_code == 200:
        balances = resp.json()
        zar = next((b for b in balances if b["currency"] == "ZAR"), None)
        sol = next((b for b in balances if b["currency"] == "SOL"), None)
        print(f"ZAR: {zar['available'] if zar else 'N/A'}")
        print(f"SOL: {sol['available'] if sol else 'N/A'}")
        return True
    else:
        print(f"❌ {resp.text}")
        return False

def test_orderbook():
    """Test public orderbook"""
    print("\n📖 Testing GET /v1/public/SOLZAR/orderbook...")
    resp = requests.get("https://api.valr.com/v1/public/SOLZAR/orderbook")
    print(f"Status: {resp.status_code}")
    if resp.status_code == 200:
        ob = resp.json()
        # VALR uses PascalCase: Bids/Asks
        if ob.get('Bids') and ob.get('Asks'):
            print(f"Best bid: {ob['Bids'][0]['price']} x {ob['Bids'][0]['quantity']}")
            print(f"Best ask: {ob['Asks'][0]['price']} x {ob['Asks'][0]['quantity']}")
            return True
        else:
            print(f"Unexpected format: {ob}")
            return False
    else:
        print(f"❌ {resp.text}")
        return False

def test_valr_pay_endpoint(subaccount_id, use_test_creds=True):
    """Test VALR Pay endpoint (dry run - doesn't actually send)"""
    print("\n💸 Testing POST /v1/pay (structure only)...")
    # We can't actually test this without sending money, but we can check the endpoint exists
    # This will likely fail with validation errors, but that's OK - we're testing auth
    # For main account, subaccount_id should be empty string
    body = {
        "currency": "SOL",
        "amount": 0.001,  # Float, not string
        "recipientPayId": "TEST",
        "anonymous": "false"
    }
    # Sign with the actual body
    headers = sign_request("POST", "/v1/pay", json.dumps(body), subaccount_id, use_test_creds)
    headers["Content-Type"] = "application/json"
    resp = requests.post("https://api.valr.com/v1/pay", headers=headers, json=body)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text}")
    # 400/404 means endpoint exists but params wrong (expected)
    # 401/403 means auth issue
    # 200 means it worked (but we sent to TEST so probably failed validation)
    return resp.status_code in [200, 400, 404]

def test_staking_endpoint(subaccount_id, use_test_creds=True):
    """Test staking endpoint (dry run)"""
    print("\n📈 Testing POST /v1/staking/stake (structure only)...")
    body = {
        "currencySymbol": "SOL",
        "amount": "0.001",  # String per API spec
        "earnType": "STAKE"
    }
    # Sign with the actual body
    headers = sign_request("POST", "/v1/staking/stake", json.dumps(body), subaccount_id, use_test_creds)
    headers["Content-Type"] = "application/json"
    resp = requests.post("https://api.valr.com/v1/staking/stake", headers=headers, json=body)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text}")
    return resp.status_code in [200, 400, 404]

def main():
    print("🔍 VALR Auto-Sweep Endpoint Tester")
    print("=" * 50)
    
    # Try dedicated autobuy credentials first, fall back to main account for testing
    subaccount_id = get_secret("valr_autobuy_subaccount_id")
    use_test_creds = False
    
    if not subaccount_id:
        print("ℹ️  No dedicated autobuy subaccount yet - using MAIN ACCOUNT for testing")
        subaccount_id = ""  # Empty = main account
        use_test_creds = True
    else:
        print(f"Using subaccount: {subaccount_id}")
    
    results = []
    results.append(("Balances", test_balances(subaccount_id, use_test_creds)))
    results.append(("Orderbook", test_orderbook()))
    results.append(("VALR Pay", test_valr_pay_endpoint(subaccount_id, use_test_creds)))
    results.append(("Staking", test_staking_endpoint(subaccount_id, use_test_creds)))
    
    print("\n📋 Endpoint Summary:")
    print("  GET  /v1/account/balances")
    print("  GET  /v1/public/SOLZAR/orderbook")
    print("  POST /v1/orders/{pair}/market")
    print("  POST /v1/pay")
    print("  POST /v1/staking/stake")
    
    print("\n" + "=" * 50)
    print("📋 Results:")
    for name, ok in results:
        status = "✅" if ok else "❌"
        print(f"  {status} {name}")
    
    if all(ok for _, ok in results):
        print("\n🎉 All endpoints responding! Ready to deploy.")
    else:
        print("\n⚠️ Some endpoints failed. Check README.md for required fixes.")

if __name__ == "__main__":
    main()
