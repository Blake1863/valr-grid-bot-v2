#!/usr/bin/env python3
"""
Cancel all open orders every 60 seconds for chart maintenance bot
Warns if any orders are found before cancelling
"""
import os, sys, requests, hmac, hashlib, time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv("/home/admin/.openclaw/secrets/cm_secrets.env")

cm1_key = os.getenv("CM1_API_KEY")
cm1_secret = os.getenv("CM1_API_SECRET")
cm2_key = os.getenv("CM2_API_KEY")
cm2_secret = os.getenv("CM2_API_SECRET")

def get_headers(key, secret, method, path):
    ts = str(int(time.time() * 1000))
    sig = hmac.new(secret.encode(), f"{ts}{method}{path}".encode(), hashlib.sha512).hexdigest()
    return {"X-VALR-API-KEY": key, "X-VALR-SIGNATURE": sig, "X-VALR-TIMESTAMP": ts}

def get_open_orders(key, secret):
    """Get all open orders for an account"""
    headers = get_headers(key, secret, "GET", "/v1/orders/open")
    response = requests.get("https://api.valr.com/v1/orders/open", headers=headers, timeout=10)
    return response.json() if response.status_code == 200 else []

def cancel_all_orders(key, secret, account_name):
    """Cancel all open orders for an account"""
    headers = get_headers(key, secret, "DELETE", "/v1/orders")
    response = requests.delete("https://api.valr.com/v1/orders", headers=headers, timeout=10)
    
    if response.status_code == 200:
        cancelled = response.json()
        count = len(cancelled) if isinstance(cancelled, list) else 0
        return count
    else:
        return 0

if __name__ == "__main__":
    print(f"[{datetime.now().isoformat()}] === Order Cleanup Check ===")
    
    # Check for open orders
    cm1_orders = get_open_orders(cm1_key, cm1_secret)
    cm2_orders = get_open_orders(cm2_key, cm2_secret)
    
    total_orders = len(cm1_orders) + len(cm2_orders)
    
    if total_orders > 0:
        print(f"⚠️  WARNING: Found {total_orders} open orders!")
        print(f"   CM1: {len(cm1_orders)} orders")
        print(f"   CM2: {len(cm2_orders)} orders")
        
        # Show order details
        for o in cm1_orders[:5]:
            print(f"   - CM1: {o['side']} {o['remainingQuantity']} {o['currencyPair']} @ {o['price']}")
        for o in cm2_orders[:5]:
            print(f"   - CM2: {o['side']} {o['remainingQuantity']} {o['currencyPair']} @ {o['price']}")
        
        if total_orders > 5:
            print(f"   ... and {total_orders - 5} more")
        
        # Cancel all
        print(f"\nCancelling all {total_orders} orders...")
        cm1_cancelled = cancel_all_orders(cm1_key, cm1_secret, "CM1")
        cm2_cancelled = cancel_all_orders(cm2_key, cm2_secret, "CM2")
        print(f"✅ Cancelled: CM1={cm1_cancelled}, CM2={cm2_cancelled}, Total={cm1_cancelled + cm2_cancelled}")
    else:
        print("✅ No open orders found - clean")
    
    print(f"[{datetime.now().isoformat()}] Cleanup check complete")
