#!/usr/bin/env python3
import asyncio, aiohttp, time, hmac, hashlib, json, sys
sys.path.insert(0, "/home/admin/.openclaw/workspace/bots/cm-bot-spot-py/src")
from creds import load
creds = load()
api_key = creds["MAIN_API_KEY"]
api_sec = creds["MAIN_API_SECRET"]

SUBS = {
    "CMSZAR1": "1513524239074144256", "CMSZAR2": "1513524288865144832",
    "CMSUSDT1": "1513524297399840768", "CMSUSDT2": "1513524305939443712",
    "CMSUSDC1": "1513524314495823872", "CMSUSDC2": "1513524323040333824",
}

async def req(session, method, path, body="", sub=None):
    ts = str(int(time.time() * 1000))
    payload = ts + method + path + body + (sub or "")
    sig = hmac.new(api_sec.encode(), payload.encode(), hashlib.sha512).hexdigest()
    headers = {"X-VALR-API-KEY": api_key, "X-VALR-SIGNATURE": sig, "X-VALR-TIMESTAMP": ts}
    if sub:
        headers["X-VALR-SUB-ACCOUNT-ID"] = sub
    if body:
        headers["Content-Type"] = "application/json"
    url = f"https://api.valr.com{path}"
    async with session.request(method, url, headers=headers, data=body or None) as r:
        txt = await r.text()
        try:
            return r.status, json.loads(txt) if txt else {}
        except:
            return r.status, {"raw": txt}

async def main():
    async with aiohttp.ClientSession() as s:
        for name, sid in SUBS.items():
            _, bals = await req(s, "GET", "/v1/account/balances", sub=sid)
            for b in bals:
                if b["currency"] in ("ZAR","USDT","USDC") and float(b.get("available",0)) > 0:
                    print(f'{name}: {b["currency"]} = {b["available"]}')
        # Main
        _, bals = await req(s, "GET", "/v1/account/balances")
        for b in bals:
            if b["currency"] in ("ZAR","USDT","USDC"):
                print(f'MAIN: {b["currency"]} = {b["available"]}')

asyncio.run(main())
