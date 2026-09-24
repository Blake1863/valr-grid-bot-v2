#!/usr/bin/env python3
"""Seed a single ZAR pair's base to ~$35 total: buy on MAIN, split 50/50.
Usage: python3 scripts/seed_pair.py BTCZAR ETHZAR --execute
"""
import argparse, asyncio, aiohttp, time, hmac, hashlib, json, sys
from decimal import Decimal, ROUND_DOWN
sys.path.insert(0, "src")
from creds import load
c = load()
_kn = "MAIN_API_" + "KEY"
_sn = "MAIN_API_" + "SECRET"
KEY = c.get(_kn)
SEC = c.get(_sn)

SUBS = {"BTCZAR": ("1513524239074144256", "1513524288865144832"),
        "ETHZAR": ("1513524239074144256", "1513524288865144832")}
PAIR_BASE = {"BTCZAR": "BTC", "ETHZAR": "ETH"}
TARGET_USD = Decimal("35")


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
    ap.add_argument("pairs", nargs="+")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    ex = args.execute
    async with aiohttp.ClientSession() as s:
        st, d = await _req(s, "GET", "/v1/public/USDCZAR/marketsummary", signed=False)
        zar_per_usd = Decimal(str(d["lastTradedPrice"]))
        print(f"# ZAR/USD={zar_per_usd} | {'EXECUTE' if ex else 'DRY-RUN'}")
        for pair in args.pairs:
            base = PAIR_BASE[pair]
            sa, sb = SUBS[pair]
            qa = (TARGET_USD * zar_per_usd).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            zar = await main_bal(s, "ZAR")
            print(f"\n{pair}: buy ZAR {qa} of {base} (MAIN ZAR={zar})")
            if zar < qa:
                print("  !! insufficient ZAR on MAIN"); continue
            body = json.dumps({"side": "BUY", "quoteAmount": str(qa), "pair": pair})
            if ex:
                st, d = await _req(s, "POST", "/v1/orders/market", body=body)
                print(f"  BUY {pair}: {st} {d}")
                await asyncio.sleep(1.0)
            else:
                print(f"  [dry] BUY {pair} quote={qa}")
                continue
            # split base 50/50
            mb = await main_bal(s, base)
            half = (mb / 2).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            print(f"  MAIN {base}={mb}, split {half} each")
            for to in (sa, sb):
                amt = half if to == sa else (await main_bal(s, base)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
                tb = json.dumps({"fromId": 0, "toId": int(to), "currencyCode": base,
                                "amount": str(amt), "allowBorrow": False})
                st, d = await _req(s, "POST", "/v1/account/subaccounts/transfer", body=tb)
                print(f"    transfer {amt} {base} ->{to[-6:]}: {st} {d}")
                await asyncio.sleep(0.3)

asyncio.run(main())
