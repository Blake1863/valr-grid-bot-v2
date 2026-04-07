#!/usr/bin/env python3
"""
Liquidate xstock inventory on CMS2 - sell all base assets for USDT.
Uses VALR Account WebSocket for order placement.
"""
import os
import json
import time
import hmac
import hashlib
import asyncio
import websockets
from datetime import datetime

# Load secrets directly without shadowing stdlib
import subprocess
result = subprocess.run(
    ['python3', '-c', '''
import sys
sys.path.insert(0, "/home/admin/.openclaw/secrets")
from secrets import get_secret
print(get_secret("valr_main_api_key"))
print(get_secret("valr_main_api_secret"))
'''],
    capture_output=True, text=True
)
api_key, api_secret = result.stdout.strip().split('\n')

# Configuration
CMS2_ID = '1483815498551132160'
WS_ACCOUNT_URL = 'wss://api.valr.com/ws/account'

# xstock pairs to liquidate (base currency)
XSTOCKS = ['SPYX', 'MSTRX', 'JUP', 'CRCLX', 'BITGOLD', 'NVDAX', 'HOODX', 'COINX', 'TSLAX', 'VALR10', 'TRUMP', 'EURC']

def sign_request(method, path, body, subaccount_id):
    """Generate VALR HMAC-SHA512 signature."""
    timestamp = str(int(time.time() * 1000))
    # Match Rust bot: timestamp + "GET/ws/account" + subaccount_id (no space between GET and path)
    message = f"{timestamp}GET{path}{subaccount_id}"
    signature = hmac.new(api_secret.encode(), message.encode(), hashlib.sha512).hexdigest()
    return timestamp, signature

async def liquidate():
    """Connect to WS account and liquidate all xstock inventory."""
    timestamp, signature = sign_request('GET', '/ws/account', '', CMS2_ID)
    
    headers = {
        'X-VALR-API-KEY': api_key,
        'X-VALR-SIGNATURE': signature,
        'X-VALR-TIMESTAMP': timestamp,
        'X-VALR-SUB-ACCOUNT-ID': CMS2_ID,
        'Host': 'api.valr.com',
        'Connection': 'Upgrade',
        'Upgrade': 'websocket',
        'Sec-WebSocket-Version': '13',
    }
    
    print(f"[{datetime.now().isoformat()}] Connecting to WS account for CMS2...")
    
    try:
        async with websockets.connect(WS_ACCOUNT_URL, additional_headers=headers) as ws:
            print(f"[{datetime.now().isoformat()}] Connected!")
            
            # Wait for AUTHENTICATED
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(msg)
                print(f"[WS] {data.get('type', 'unknown')}")
                if data.get('type') == 'AUTHENTICATED':
                    break
            
            # Subscribe to balance updates
            await ws.send(json.dumps({
                'type': 'SUBSCRIBE',
                'subscriptions': [{'event': 'BALANCE_UPDATE'}]
            }))
            
            # Collect balances
            balances = {}
            print(f"[{datetime.now().isoformat()}] Waiting for balance updates (5s)...")
            end_time = time.time() + 5
            while time.time() < end_time:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1)
                    data = json.loads(msg)
                    if data.get('type') == 'BALANCE_UPDATE':
                        for b in data.get('data', []):
                            curr = b.get('currency', '')
                            avail = float(b.get('availableInReference', 0) or b.get('available', 0) or 0)
                            if avail > 0 and curr in XSTOCKS:
                                balances[curr] = avail
                                print(f"  {curr}: {avail}")
                except asyncio.TimeoutError:
                    break
            
            if not balances:
                print(f"[{datetime.now().isoformat()}] No xstock inventory found on CMS2")
                return
            
            print(f"\n[{datetime.now().isoformat()}] Placing market sell orders...")
            
            # Place market sell orders for each xstock
            for base_currency, quantity in balances.items():
                pair = f"{base_currency}USDT"
                client_msg_id = f"liquidate-{int(time.time()*1000)}-{base_currency}"
                customer_order_id = f"liq-{base_currency}-{int(time.time()*1000)}"
                
                # Market sell: IOC, no price
                payload = {
                    'side': 'sell',
                    'quantity': f"{quantity:.8f}",
                    'pair': pair,
                    'timeInForce': 'IOC',
                    'customerOrderId': customer_order_id
                }
                
                order_msg = {
                    'type': 'PLACE_LIMIT_ORDER',
                    'clientMsgId': client_msg_id,
                    'payload': payload
                }
                
                print(f"  Selling {quantity} {base_currency} @ market")
                await ws.send(json.dumps(order_msg))
                
                # Wait for response
                try:
                    resp = await asyncio.wait_for(ws.recv(), timeout=5)
                    resp_data = json.loads(resp)
                    print(f"    → {resp_data.get('type')}: {resp_data.get('data', resp_data)}")
                except asyncio.TimeoutError:
                    print(f"    → Timeout waiting for response")
                
                await asyncio.sleep(0.5)
            
            print(f"\n[{datetime.now().isoformat()}] Liquidation complete!")
            
    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(liquidate())
