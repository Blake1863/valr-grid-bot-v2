#!/usr/bin/env python3
"""One-shot balance audit across all 6 wash subaccounts."""
import asyncio, aiohttp, time, hmac, hashlib, sys
sys.path.insert(0, "src")
from creds import load

creds = load()
KEY = creds["MAIN_API_KEY"]
SEC = creds["MAIN_API_SECRET"]

SUBS = {
    "CMSZAR1":  "1513524239074144256",
    "CMSZAR2":  "1513524288865144832",
    "CMSUSDT1": "1513524297399840768",
    "CMSUSDT2": "1513524305939443712",
    "CMSUSDC1": "1513524314495823872",
    "CMSUSDC2": "1513524323040333824",
}

async def bal(session, sid):
    ts = str(int(time.time() * 1000))
    path = "/v1/account/balances"
    payload = ts + "GET" + path + "" + sid
    sig = hmac.new(SEC.encode(), payload.encode(), hashlib.sha512).hexdigest()
    headers = {"X-VALR-API-KEY": KEY, "X-VALR-SIGNATURE": sig,
               "X-VALR-TIMESTAMP": ts, "X-VALR-SUB-ACCOUNT-ID": sid}
    async with session.get(f"https://api.valr.com{path}", headers=headers) as r:
        return await r.json()

async def main():
    async with aiohttp.ClientSession() as s:
        for name, sid in SUBS.items():
            data = await bal(s, sid)
            print(f"=== {name} ===")
            if isinstance(data, list):
                for it in data:
                    av = float(it.get("available", "0") or 0)
                    tot = float(it.get("total", "0") or 0)
                    if tot > 0:
                        print(f"  {it['currency']}: avail={av} total={tot}")
            else:
                print("  ERR", data)
            print()
            await asyncio.sleep(0.15)

asyncio.run(main())
