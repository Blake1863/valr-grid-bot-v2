#!/usr/bin/env python3
"""Retry base transfers to subs with rounding-safe amounts."""
import asyncio, aiohttp, time, hmac, hashlib, json, sys
from decimal import Decimal, ROUND_DOWN
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

BUCKET_SUBS = {
    "zar": ["CMSZAR1", "CMSZAR2"],
    "usdt": ["CMSUSDT1", "CMSUSDT2"],
    "usdc": ["CMSUSDC1", "CMSUSDC2"],
}

# Main balances from last check (leaving some dust in MAIN)
MAIN_BALS = {
    "AVAX": 2.78, "USDC": 19.0,
}

# Pairs we still need to transfer (ZAR pairs didn't land in subs)
# All the ZAR bucket buys went through, but transfers returned 202 (async) and nothing arrived
# Also need to transfer XAUT which had 403 on second sub
NEED_TRANSFER = [
    ("AVAX", "zar", "2.78"),
    ("USDC", "zar", "35"),  # bought 35 USDCZAR, MAIN should have ~39.4
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
        # First get actual main balances for all base currencies we bought
        status, bals = await req(s, "GET", "/v1/account/balances")
        if status != 200:
            print(f"Failed to get balances: {bals}")
            return
        all_bals = {b["currency"]: float(b.get("available", 0)) for b in bals}

        # Check ZAR sub balances for the currencies we bought
        for sub_name in ["CMSZAR1", "CMSZAR2"]:
            sub_id = SUBS[sub_name]
            status, sub_bals = await req(s, "GET", "/v1/account/balances", sub=sub_id)
            if status == 200:
                found = {b["currency"]: float(b.get("available",0)) for b in sub_bals}
                print(f"\n{sub_name}:")
                for c in ["AVAX","BNB","BTC","ETH","LINK","SOL","USDC","XAUT","XRP"]:
                    print(f"  {c}: {found.get(c, 0)}")

        # Get MAIN balances for all currencies we bought
        print("\nMAIN available for transfer:")
        for c in ["AVAX","BNB","BTC","ETH","LINK","SOL","USDC","XAUT","XRP",
                  "BITGOLD","COINX","CRCLX","HOODX","JUP","MSTRX","NVDAX",
                  "PUMP","TRUMP","TSLAX","USDPC","VALR10"]:
            if all_bals.get(c, 0) > 0.000001:
                print(f"  {c}: {all_bals[c]}")

        # Transfer ZAR pair base currencies
        zar_currencies = ["AVAX","BNB","BTC","ETH","LINK","SOL","USDC","XAUT","XRP"]
        bucket = "zar"
        subs = BUCKET_SUBS[bucket]

        for currency in zar_currencies:
            avail = all_bals.get(currency, 0)
            if avail < 0.00001:
                print(f"\nSkipping {currency}: insufficient MAIN balance ({avail})")
                continue

            # Split 45/45, leave 10% in main to avoid rounding issues
            half = avail * 0.45
            # Truncate to 8 decimal places to avoid rounding rejects
            half_str = f"{half:.8f}"

            print(f"\nTransferring {currency} (MAIN avail={avail}):")
            for sub_name in subs:
                sub_id = SUBS[sub_name]
                body = json.dumps({
                    "fromId": "0",
                    "toId": sub_id,
                    "currencyCode": currency,
                    "amount": half_str,
                    "allowBorrow": False,
                })
                status, resp = await req(s, "POST", "/v1/account/subaccounts/transfer", body=body)
                print(f"  -> {sub_name}: status={status} {resp}")
                await asyncio.sleep(1)

asyncio.run(main())
