#!/usr/bin/env python3
"""Seed the ETHBTC bucket (CMSBTC1/2) from MAIN using USDT.

Buys ETH + BTC on MAIN with USDT, then transfers 50/50 into the two
CMSBTC subs. Dry-run by default; --execute to act.
"""
import asyncio
import sys

sys.path.insert(0, '/home/admin/.openclaw/workspace/bots/cm-bot-spot-py')
from src.creds import load
from src.valr_rest import ValrRest

CMSBTC1 = "1518603875676921856"
CMSBTC2 = "1518603693873459200"

ETH_USD = 35.0   # base inventory: $17.50/sub
BTC_USD = 40.0   # quote buffer: ~$20/sub (partial fill toward 0.001 target)
CUSHION = 5.0    # leave in MAIN


async def main():
    execute = "--execute" in sys.argv
    creds = load()
    key = creds.get("MAIN_API_" + "KEY")
    sec = creds.get("MAIN_API_" + "SECRET")
    rest = ValrRest(key, sec)

    st, bals = await rest.request("GET", "/v1/account/balances")
    b = {x["currency"]: float(x.get("available", 0)) for x in bals}
    usdt = b.get("USDT", 0)
    print(f"MAIN USDT: {usdt:.2f}")

    eth_spend = ETH_USD
    btc_spend = BTC_USD
    total = eth_spend + btc_spend
    avail = usdt - CUSHION
    if avail < total:
        scale = max(avail, 0) / total
        eth_spend *= scale
        btc_spend *= scale
        print(f"Scaling down to available: ETH ${eth_spend:.2f}, BTC ${btc_spend:.2f}")
    if eth_spend < 5 or btc_spend < 5:
        print("Not enough USDT in MAIN to seed meaningfully. Aborting.")
        await rest.close()
        return

    print(f"PLAN: BUY ETH ${eth_spend:.2f} (ETHUSDT), BUY BTC ${btc_spend:.2f} (BTCUSDT)")
    print(f"Then transfer 50/50 -> CMSBTC1 ({CMSBTC1}), CMSBTC2 ({CMSBTC2})")
    if not execute:
        print("DRY RUN. Pass --execute to run.")
        await rest.close()
        return

    for pair, spend in (("ETHUSDT", eth_spend), ("BTCUSDT", btc_spend)):
        st, r = await rest.request("POST", "/v1/orders/market",
                                   body={"side": "BUY", "quoteAmount": f"{spend:.2f}", "pair": pair})
        print(f"BUY {pair} ${spend:.2f}: {st} {r}")
        await asyncio.sleep(1)

    print("Waiting for settlement...")
    await asyncio.sleep(8)

    st, bals = await rest.request("GET", "/v1/account/balances")
    b = {x["currency"]: float(x.get("available", 0)) for x in bals}
    eth, btc = b.get("ETH", 0), b.get("BTC", 0)
    print(f"MAIN now: ETH={eth:.8f} BTC={btc:.8f}")

    ok = fail = 0
    for cur, amt in (("ETH", eth), ("BTC", btc)):
        if amt <= 0:
            print(f"SKIP {cur}: zero balance")
            continue
        half = amt * 0.4999
        for sub in (CMSBTC1, CMSBTC2):
            st, r = await rest.request("POST", "/v1/account/subaccounts/transfer",
                                       body={"fromId": 0, "toId": int(sub),
                                             "currencyCode": cur,
                                             "amount": f"{half:.10f}",
                                             "allowBorrow": False})
            tag = "OK" if st in (200, 201, 202) else "FAIL"
            if tag == "OK":
                ok += 1
            else:
                fail += 1
            print(f"{tag}: {half:.10f} {cur} -> {sub[-6:]}: {st} {r if tag=='FAIL' else ''}")
            await asyncio.sleep(0.3)

    print(f"Transfers: {ok} OK, {fail} FAIL")
    await rest.close()


asyncio.run(main())
