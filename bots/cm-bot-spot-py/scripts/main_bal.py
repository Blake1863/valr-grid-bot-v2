#!/usr/bin/env python3
import asyncio, aiohttp, time, hmac, hashlib, sys
sys.path.insert(0, "src")
from creds import load
c = load()
KEY = c.get("MAIN_API_" + "KEY"); SEC = c.get("MAIN_API_" + "SECRET")

async def main():
    async with aiohttp.ClientSession() as s:
        ts = str(int(time.time()*1000)); path="/v1/account/balances"
        sig = hmac.new(SEC.encode(),(ts+"GET"+path+"").encode(),hashlib.sha512).hexdigest()
        h={"X-VALR-API-KEY":KEY,"X-VALR-SIGNATURE":sig,"X-VALR-TIMESTAMP":ts}
        async with s.get(f"https://api.valr.com{path}",headers=h) as r:
            d=await r.json()
        print("=== MAIN (primary) ===")
        for it in d:
            av=float(it.get("available","0") or 0)
            if av>0 and it["currency"] in ("ZAR","USDT","USDC","ETH","SOL","XAUT","EURC"):
                print(f"  {it['currency']}: {av}")
asyncio.run(main())
