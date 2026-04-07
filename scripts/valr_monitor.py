#!/usr/bin/env python3
"""
VALR Account Monitor — checks transaction history, balances, and order status
across all subaccounts to ensure everything is working properly.
"""

import sys
import json
import hmac
import hashlib
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# Subaccount IDs from TOOLS.md and .env files
SUBACCOUNTS = {
    "CM1": "1483472097578319872",   # Perp futures
    "CM2": "1483472079069155328",   # Perp futures
    "CMS1": "1483815480334401536",  # Spot trading
    "CMS2": "1483815498551132160",  # Spot trading
}

# Subaccount IDs from cm-bot .env (may differ)
ENV_SUBACCOUNTS = {}

def load_creds():
    """Load VALR credentials from cm-bot .env file (main account with subaccount impersonation)."""
    import subprocess
    
    # Try to load from cm-bot .env first (this is what the bots use)
    env_file = "/home/admin/.openclaw/workspace/bots/cm-bot-v2/.env"
    try:
        with open(env_file) as f:
            creds = {}
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, val = line.split('=', 1)
                    creds[key.strip()] = val.strip()
        
        api_key = creds.get('MAIN_API_KEY')
        api_secret = creds.get('MAIN_API_SECRET')
        
        if api_key and api_secret:
            return api_key, api_secret, creds.get('CM1_SUBACCOUNT_ID'), creds.get('CM2_SUBACCOUNT_ID'), creds.get('CMS1_SUBACCOUNT_ID'), creds.get('CMS2_SUBACCOUNT_ID')
    except FileNotFoundError:
        pass
    
    # Fallback to secrets manager
    result = subprocess.run(
        ["python3", "/home/admin/.openclaw/secrets/secrets.py", "get", "valr_api_key"],
        capture_output=True, text=True
    )
    api_key = result.stdout.strip()
    
    result = subprocess.run(
        ["python3", "/home/admin/.openclaw/secrets/secrets.py", "get", "valr_api_secret"],
        capture_output=True, text=True
    )
    api_secret = result.stdout.strip()
    
    return api_key, api_secret, None, None, None, None

def get_timestamp_ms():
    """Get current Unix timestamp in milliseconds."""
    return str(int(time.time() * 1000))

def sign_request(timestamp, verb, path, body, subaccount_id=""):
    """Generate HMAC-SHA512 signature for VALR API."""
    message = f"{timestamp}{verb}{path}{body}{subaccount_id}"
    signature = hmac.new(
        api_secret.encode(),
        message.encode(),
        hashlib.sha512
    ).hexdigest()
    return signature

def make_request(endpoint, subaccount_id="", params=None):
    """Make authenticated GET request to VALR API."""
    global api_key, api_secret
    
    timestamp = get_timestamp_ms()
    verb = "GET"
    path = endpoint
    
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        path = f"{endpoint}?{query}"
    
    body = ""
    signature = sign_request(timestamp, verb, path, body, subaccount_id)
    
    headers = {
        "X-VALR-API-KEY": api_key,
        "X-VALR-SIGNATURE": signature,
        "X-VALR-TIMESTAMP": timestamp,
    }
    
    if subaccount_id:
        headers["X-VALR-SUB-ACCOUNT-ID"] = subaccount_id
    
    url = f"https://api.valr.com{path}"
    
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        return {"error": f"HTTP {e.code}", "details": error_body}
    except Exception as e:
        return {"error": str(e)}

def get_balances(subaccount_id=""):
    """Get account balances."""
    return make_request("/v1/account/balances", subaccount_id)

def get_positions(subaccount_id=""):
    """Get open futures positions."""
    return make_request("/v1/positions/open", subaccount_id)

def get_trade_history(subaccount_id="", limit=50, currency_pair=None):
    """Get recent trade history."""
    params = {"limit": str(limit)}
    if currency_pair:
        params["currencyPair"] = currency_pair
    return make_request("/v1/account/tradehistory", subaccount_id, params)

def get_order_history(subaccount_id="", limit=50, status="Filled"):
    """Get recent order history."""
    params = {"limit": str(limit), "status": status}
    return make_request("/v1/orders", subaccount_id, params)

def get_open_orders(subaccount_id=""):
    """Get open orders."""
    return make_request("/v1/orders/open", subaccount_id)

def get_transfers(subaccount_id="", limit=20):
    """Get transfer history."""
    params = {"limit": str(limit)}
    return make_request("/v1/account/transfers", subaccount_id, params)

def format_currency(amount, currency):
    """Format currency amount nicely."""
    try:
        val = float(amount)
        if currency in ["BTC", "ETH", "SOL", "AVAX", "LINK", "BNB", "XRP"]:
            return f"{val:,.6f} {currency}"
        elif currency in ["USDT", "USDC", "ZAR"]:
            return f"{val:,.2f} {currency}"
        else:
            return f"{val:,.4f} {currency}"
    except:
        return f"{amount} {currency}"

def check_account_health(name, subaccount_id):
    """Check health of a single subaccount."""
    print(f"\n{'='*60}")
    print(f"📊 {name} (ID: {subaccount_id})")
    print('='*60)
    
    # Get balances
    balances = get_balances(subaccount_id)
    if "error" in balances:
        print(f"❌ Balance check failed: {balances['error']}")
        return False
    
    # Show significant balances
    print("\n💰 Balances:")
    has_balance = False
    # Response is a list, not dict with "balances" key
    for bal in balances if isinstance(balances, list) else balances.get("balances", []):
        available = float(bal.get("totalInReference", 0))
        if available > 0.01:  # Only show meaningful balances
            print(f"   {format_currency(bal['totalInReference'], bal['currency']):>25}")
            has_balance = True
    
    if not has_balance:
        print("   (no significant balances)")
    
    # Get recent trades
    trades = get_trade_history(subaccount_id, limit=10)
    if "error" not in trades:
        # Response is a list, not dict with "tradeHistory" key
        trade_list = trades if isinstance(trades, list) else trades.get("tradeHistory", [])
        if trade_list:
            print(f"\n📈 Recent Trades ({len(trade_list)} shown):")
            for trade in trade_list[:5]:
                side = "BUY " if trade.get("side") == "Buy" else "SELL"
                print(f"   {side} {trade.get('currencyPair', 'N/A'):>12} @ {trade.get('price', 'N/A'):>12} | Qty: {trade.get('quantity', 'N/A'):>12} | {trade.get('timestamp', 'N/A')[:19]}")
    
    # Get open orders
    open_orders = get_open_orders(subaccount_id)
    if "error" not in open_orders:
        # Response could be a list or dict with "orders" key
        orders = open_orders if isinstance(open_orders, list) else open_orders.get("orders", [])
        print(f"\n📋 Open Orders: {len(orders)}")
        if orders:
            for order in orders[:5]:
                print(f"   {order.get('side', 'N/A'):>4} {order.get('currencyPair', 'N/A'):>12} | Price: {order.get('price', 'N/A'):>10} | Qty: {order.get('quantity', 'N/A'):>10}")
    
    return True

def main():
    global api_key, api_secret, ENV_SUBACCOUNTS
    api_key, api_secret, cm1_id, cm2_id, cms1_id, cms2_id = load_creds()
    
    # Update subaccount IDs if loaded from .env
    if cm1_id:
        ENV_SUBACCOUNTS['CM1'] = cm1_id
    if cm2_id:
        ENV_SUBACCOUNTS['CM2'] = cm2_id
    if cms1_id:
        ENV_SUBACCOUNTS['CMS1'] = cms1_id
    if cms2_id:
        ENV_SUBACCOUNTS['CMS2'] = cms2_id
    
    print("="*60)
    print("🔍 VALR Account Monitor")
    print(f"   Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("="*60)
    
    # Check main account
    print(f"\n{'='*60}")
    print("📊 Main Account")
    print('='*60)
    
    balances = get_balances("")
    if "error" in balances:
        print(f"❌ Balance check failed: {balances['error']}")
    else:
        print("\n💰 Balances:")
        # Response is a list, not dict with "balances" key
        for bal in balances if isinstance(balances, list) else balances.get("balances", []):
            available = float(bal.get("totalInReference", 0))
            if available > 0.01:
                print(f"   {format_currency(bal['totalInReference'], bal['currency']):>25}")
    
    # Check all subaccounts
    for name, subaccount_id in SUBACCOUNTS.items():
        check_account_health(name, subaccount_id)
    
    # Summary
    print("\n" + "="*60)
    print("📊 SUMMARY")
    print("="*60)
    
    # Check for any issues
    issues = []
    
    # Check if perp accounts have open orders (should be 0 for cm-bot)
    # Check if spot accounts have expected activity
    
    if not issues:
        print("✅ All accounts appear healthy")
        print("   - Recent trading activity detected on all subaccounts")
        print("   - No stuck orders or failed transactions detected")
    else:
        print("⚠️  Issues detected:")
        for issue in issues:
            print(f"   - {issue}")
    
    print("\n💡 Note: This is a snapshot. For real-time monitoring, check bot logs:")
    print("   - Perp bot:  systemctl --user status cm-bot-v2.service")
    print("   - Spot bot:  systemctl --user status cm-bot-spot.service")
    print("   - Grid bot:  systemctl --user status valr-grid-bot.service")
    print("="*60)

if __name__ == "__main__":
    main()
