#!/usr/bin/env python3
import asyncio, aiohttp, time, hmac, hashlib, json, sys
sys.path.insert(0, "/home/admin/.openclaw/workspace/bots/cm-bot-spot-py")
from src.creds import load
creds = load()
KEY = creds.get("MAIN_API_" + "KEY")
SEC = creds.get("MAIN_API_" + "SECRET")

SUBS = {
    "CMSZAR1": "1513524239074144256", "CMSZAR2": "1513524288865144832",
    "CMSUSDT1": "1513524297399840768", "CMSUSDT2": "1513524305939443712",
    "CMSUSDC1": "1513524314495823872", "CMSUSDC2": "1513524323040333824",
}

BASE_CURRENCIES = ["AVAX","BNB","BTC","ETH","LINK","SOL","USDC","XAUT","XRP",
                   "BITGOLD","COINX","CRCLX","HOODX","JUP","MSTRX","NVDAX",
                   "PUMP","TRUMP","TSLAX","USDPC","VALR10"]

async def req(session, method, path, body="", sub=None):
    ts = str(int(time.time() * 1000))
    payload = ts + method + path + body + (sub or "")
    sig = hmac.new(SEC.encode(), payload.encode(), hashlib.sha512).hexdigest()
    headers = {"X-VALR-API-KEY": KEY, "X-VALR-SIGNATURE": sig, "X-VALR-TIMESTAMP": ts}
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
        # Check sub balances
        for sub_name, sub_id in SUBS.items():
            status, bals = await req(s, "GET", "/v1/account/balances", sub=sub_id)
            if status == 200:
                found = {b["currency"]: float(b.get("available",0)) for b in bals if float(b.get("available",0)) > 0}
                print(f"{sub_name} ({sub_id}): {found if found else '(empty)'}")
            await asyncio.sleep(0.5)

        # Check main remaining
        status, bals = await req(s, "GET", "/v1/account/balances")
        if status == 200:
            main_bals = {b["currency"]: float(b.get("available",0)) for b in bals if b["currency"] in BASE_CURRENCIES and float(b.get("available",0)) > 0}
            print(f"\nMAIN remaining base: {main_bals}")

asyncio.run(main())
