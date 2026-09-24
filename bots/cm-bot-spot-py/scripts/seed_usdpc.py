#!/usr/bin/env python3
"""Seed USDPCUSDT base to ~$35 total: buy on MAIN with USDT, split 50/50 to USDT subs."""
import argparse, asyncio, aiohttp, time, hmac, hashlib, json, sys
from decimal import Decimal, ROUND_DOWN
sys.path.insert(0, "src")
from creds import load
c = load()
_kn = "MAIN_API_" + "KEY"
_sn = "MAIN_API_" + "SECRET"
KEY = c[_kn]
SEC = c[_sn]

PAIR = "USDPCUSDT"
BASE = "USDPC"
SUB_A = "1513524297399840768"  # CMSUSDT1
SUB_B = "1513524305939443712"  # CMSUSDT2
TARGET_USDT = Decimal("35")    # USDT quote spend (~$35 of base)


async def _req(s, method, path, body="", signed=True, sub=None):
    ts = str(int(time.time() * 1000))
    headers = {}
    if signed:
        payload = ts + method + path + body + (sub or "")
        sig = hmac.new(SEC.encode(), payload.encode(), hashlib.sha512).hexdigest()
        headers = {"X-VALR-API-KEY": KEY, "X-VALR-SIGNATURE": sig, "X-VALR-TIMESTAMP": ts}
        if sub:
            headers["X-VALR-SUB-ACCOUNT-ID"] = sub
    if body:
        headers["Content-Type"] = "application/json"
    async with s.request(method, f"https://api.valr.com{path}", headers=headers, data=body or None) as r:
        t = await r.text()
        try:
            return r.status, json.loads(t)
        except Exception:
            return r.status, t


async def main_bal(s, cur):
    st, d = await _req(s, "GET", "/v1/account/balances")
    for it in d:
        if it["currency"] == cur:
            return Decimal(str(it.get("available", "0") or "0"))
    return Decimal(0)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    ex = args.execute
    async with aiohttp.ClientSession() as s:
        usdt = await main_bal(s, "USDT")
        print(f"# MAIN USDT={usdt} | {'EXECUTE' if ex else 'DRY-RUN'}")
        if usdt < TARGET_USDT:
            print("  !! insufficient USDT on MAIN"); return
        body = json.dumps({"side": "BUY", "quoteAmount": str(TARGET_USDT), "pair": PAIR})
        if not ex:
            print(f"  [dry] BUY {PAIR} quote={TARGET_USDT}, then split 50/50"); return
        st, d = await _req(s, "POST", "/v1/orders/market", body=body)
        print(f"  BUY {PAIR}: {st} {d}")
        await asyncio.sleep(1.2)
        mb = await main_bal(s, BASE)
        half = (mb / 2).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        print(f"  MAIN {BASE}={mb}, split ~{half} each")
        for to in (SUB_A, SUB_B):
            amt = half if to == SUB_A else (await main_bal(s, BASE)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            tb = json.dumps({"fromId": 0, "toId": int(to), "currencyCode": BASE,
                            "amount": str(amt), "allowBorrow": False})
            st, d = await _req(s, "POST", "/v1/account/subaccounts/transfer", body=tb)
            print(f"    transfer {amt} {BASE} ->{to[-6:]}: {st} {d}")
            await asyncio.sleep(0.3)

asyncio.run(main())
