#!/usr/bin/env python3
"""Retry USDT pair base transfers to CMSUSDT1/2."""
import asyncio, aiohttp, time, hmac, hashlib, json, sys
from decimal import Decimal, ROUND_DOWN
sys.path.insert(0, "/home/admin/.openclaw/workspace/bots/cm-bot-spot-py")
from src.creds import load
creds = load()
KEY = creds.get("MAIN_API_" + "KEY")
SEC = creds.get("MAIN_API_" + "SECRET")

SUBS = {
    "CMSUSDT1": "1513524297399840768", "CMSUSDT2": "1513524305939443712",
}

USDT_CURRENCIES = [
    "BITGOLD","COINX","CRCLX","HOODX","JUP","MSTRX","NVDAX",
    "PUMP","TRUMP","TSLAX","USDPC","VALR10","XAUT"
]

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
        # Get MAIN balances
        status, bals = await req(s, "GET", "/v1/account/balances")
        if status != 200:
            print(f"Failed: {bals}")
            return
        all_bals = {b["currency"]: float(b.get("available", 0)) for b in bals}

        # Print USDT sub current state
        for sub_name, sub_id in SUBS.items():
            _, sub_bals = await req(s, "GET", "/v1/account/balances", sub=sub_id)
            found = {b["currency"]: float(b.get("available",0)) for b in sub_bals if b["currency"] in USDT_CURRENCIES}
            print(f"{sub_name}: {found}")

        # Transfer each currency
        for currency in USDT_CURRENCIES:
            avail = all_bals.get(currency, 0)
            if avail < 0.000001:
                print(f"\n{currency}: no balance in MAIN")
                continue

            # Use 45% each, leave 10% in main
            half = avail * 0.45
            # Truncate to safe precision
            if avail < 0.01:
                half_str = f"{half:.10g}"
            else:
                half_str = f"{half:.8f}"

            print(f"\n{currency} (MAIN={avail}): transfer {half_str} each")
            for sub_name, sub_id in SUBS.items():
                body = json.dumps({
                    "fromId": "0", "toId": sub_id,
                    "currencyCode": currency, "amount": half_str, "allowBorrow": False,
                })
                status, resp = await req(s, "POST", "/v1/account/subaccounts/transfer", body=body)
                print(f"  -> {sub_name}: {status} {resp}")
                await asyncio.sleep(1)

asyncio.run(main())
