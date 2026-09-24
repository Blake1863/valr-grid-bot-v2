#!/usr/bin/env python3
"""Push ZAR from MAIN to CMSZAR1/CMSZAR2 subs."""
import asyncio, aiohttp, time, hmac, hashlib, json, sys
sys.path.insert(0, "src")
from creds import load

creds = load()
KEY = creds.get("MAIN_API_" + "KEY")
SEC = creds.get("MAIN_API_" + "SECRET")
CMSZAR1 = "1513524239074144256"
CMSZAR2 = "1513524288865144832"

def sign(method, path, body=""):
    ts = str(int(time.time() * 1000))
    payload = ts + method + path + body
    sig = hmac.new(SEC.encode(), payload.encode(), hashlib.sha512).hexdigest()
    return ts, sig

async def transfer(session, to_id, amount):
    path = "/v1/account/subaccounts/transfer"
    body = {"fromId": 0, "toId": int(to_id), "currencyCode": "ZAR", "amount": str(amount), "allowBorrow": False}
    body_json = json.dumps(body)
    h = {"X-VALR-API-KEY": KEY, "Content-Type": "application/json"}
    ts, sig = sign("POST", path, body_json)
    h["X-VALR-TIMESTAMP"] = ts
    h["X-VALR-SIGNATURE"] = sig
    async with session.post("https://api.valr.com" + path, headers=h, data=body_json) as r:
        result = await r.json()
        print(f"  {'ok' if r.status == 200 else 'FAIL'} ZAR {amount} -> {to_id}: HTTP {r.status}")
        if r.status != 200:
            print(f"    {result}")

async def main():
    half = 57.90
    async with aiohttp.ClientSession() as s:
        print("Pushing ZAR to subs:")
        await transfer(s, CMSZAR1, half)
        await asyncio.sleep(1)
        await transfer(s, CMSZAR2, half)

asyncio.run(main())
