#!/usr/bin/env python3
"""
quick_restock.py — Buy thin pairs directly on each sub using available quote.
No MAIN transfer needed. Market BUY with quoteAmount on each sub.

Pairs to restock:
  ZAR:  BTCZAR, ETHZAR, XRPZAR  on CMSZAR1 + CMSZAR2
  USDT: COINXUSDT on CMSUSDT1 + CMSUSDT2

Sizing: ~$35/pair total, split ~$17.50/sub (~R290/sub for ZAR, ~$17.50/sub for USDT)
"""
import asyncio, json, time, hmac, hashlib, sys
sys.path.insert(0, "src")
from creds import load

c = load()
KEY = c["MAIN_API_" + "KEY"]
SEC = c["MAIN_API_" + "SECRET"]

# Sub IDs from config files (2026-06 normalization era)
CMSZAR1 = "1513524239074144256"   # CMSZAR1
CMSZAR2 = "1513524288865144832"   # CMSZAR2
CMSUSDT1 = "1513524297399840768"  # CMSUSDT1
CMSUSDT2 = "1513524305939443712"  # CMSUSDT2

ZAR_SPEND_PER_SUB = 390  # ~R390 each sub for ZAR pairs (BTC+ETH+XRP split)
# Actually let's do per-pair: ~R196 BTC, ~R292 ETH, ~R290 XRP per sub
# Total per ZAR sub: ~R778 out of R2908+ — fine

USDT_SPEND_COINX = 17.5  # per sub

async def market_buy(pair: str, quote_amount: str, sub_id: str) -> dict:
    """POST /v1/orders/market on subaccount."""
    import aiohttp
    ts = str(int(time.time() * 1000))
    body = json.dumps({"side": "BUY", "quoteAmount": quote_amount, "pair": pair})
    path = "/v1/orders/market"
    payload = ts + "POST" + path + body + sub_id
    sig = hmac.new(SEC.encode(), payload.encode(), hashlib.sha512).hexdigest()
    headers = {
        "X-VALR-API-KEY": KEY,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
        "X-VALR-SUB-ACCOUNT-ID": sub_id,
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(f"https://api.valr.com{path}", headers=headers, data=body) as r:
            resp = await r.json()
    return resp

async def main():
    # ZAR pairs: split spend evenly between subs
    # BTCZAR: ~R196/sub, ETHZAR: ~R292/sub, XRPZAR: ~R290/sub
    zar_pairs = {
        "BTCZAR": "196",
        "ETHZAR": "292",
        "XRPZAR": "290",
    }

    results = []
    for pair, spend in zar_pairs.items():
        for sub_id, label in [(CMSZAR1, "CMSZAR1"), (CMSZAR2, "CMSZAR2")]:
            print(f"  BUY {pair} on {label}: quoteAmount={spend} ZAR")
            try:
                r = await market_buy(pair, spend, sub_id)
                if "orderId" in r:
                    print(f"    ✅ {r.get('orderId')} side={r.get('side')} pair={r.get('pair')}")
                else:
                    print(f"    ❌ {json.dumps(r)[:200]}")
                results.append({"pair": pair, "sub": label, "ok": "orderId" in r, "resp": r})
            except Exception as e:
                print(f"    ❌ Exception: {e}")
                results.append({"pair": pair, "sub": label, "ok": False, "error": str(e)})

    # COINXUSDT on USDT subs
    for sub_id, label in [(CMSUSDT1, "CMSUSDT1"), (CMSUSDT2, "CMSUSDT2")]:
        print(f"  BUY COINXUSDT on {label}: quoteAmount={USDT_SPEND_COINX} USDT")
        try:
            r = await market_buy("COINXUSDT", str(USDT_SPEND_COINX), sub_id)
            if "orderId" in r:
                print(f"    ✅ {r.get('orderId')} side={r.get('side')} pair={r.get('pair')}")
            else:
                print(f"    ❌ {json.dumps(r)[:200]}")
            results.append({"pair": "COINXUSDT", "sub": label, "ok": "orderId" in r, "resp": r})
        except Exception as e:
            print(f"    ❌ Exception: {e}")
            results.append({"pair": "COINXUSDT", "sub": label, "ok": False, "error": str(e)})

    ok = sum(1 for r in results if r["ok"])
    fail = len(results) - ok
    print(f"\nDone: {ok} succeeded, {fail} failed")
    return results

asyncio.run(main())
