#!/usr/bin/env python3
"""Sell all remaining base dust to quote, transfer everything to MAIN."""
import asyncio, sys, json

sys.path.insert(0, "src")
from creds import load
from valr_rest import ValrRest

EXECUTE = "--execute" in sys.argv

SUBS = {
    "CMSZAR1":  "1513524239074144256",
    "CMSZAR2":  "1513524288865144832",
    "CMSUSDT1": "1513524297399840768",
    "CMSUSDT2": "1513524305939443712",
    "CMSUSDC1": "1513524314495823872",
    "CMSUSDC2": "1513524323040333824",
}
MAIN_ID = 0

ZAR_PAIRS = {"BTC":"BTCZAR","ETH":"ETHZAR","SOL":"SOLZAR","XRP":"XRPZAR",
             "AVAX":"AVAXZAR","BNB":"BNBZAR","LINK":"LINKZAR","XAUT":"XAUTZAR"}
USDT_PAIRS = {"JUP":"JUPUSDT","TRUMP":"TRUMPUSDT","HOODX":"HOODXUSDT",
              "SPYX":"SPYXUSDT","VALR10":"VALR10USDT","TSLAX":"TSLAXUSDT",
              "CRCLX":"CRCLXUSDT","COINX":"COINXUSDT","MSTRX":"MSTRXUSDT",
              "BITGOLD":"BITGOLDUSDT","NVDAX":"NVDAXUSDT","PUMP":"PUMPUSDT",
              "XAUT":"XAUTUSDT"}

async def sell_and_transfer(rest, sub_id, sub_name, pair_map, quote_cur):
    """Sell all base dust, then buy USDT if on ZAR, then transfer all to MAIN."""
    bals = {b["currency"]: b for b in await rest.balances(sub_id)}

    # Sell base dust
    print(f"\n=== {sub_name}: sell base dust ===")
    for asset, pair in pair_map.items():
        avail = float(bals.get(asset, {}).get("available", 0))
        if avail > 0:
            amt = avail * 0.99  # sell 99%
            print(f"  SELL {amt} {asset} on {pair}")
            if EXECUTE:
                st, d = await rest.request("POST", "/v1/orders/market",
                    {"pair": pair, "side": "SELL", "baseAmount": str(amt), "timeInForce": "IOC"},
                    subaccount_id=sub_id)
                print(f"    {st}")
                await asyncio.sleep(0.3)
            else:
                print("    [DRY RUN]")

    # Refresh
    await asyncio.sleep(2)
    bals = {b["currency"]: b for b in await rest.balances(sub_id)}

    # ZAR subs: buy USDT with ZAR
    if quote_cur == "ZAR":
        zar = float(bals.get("ZAR", {}).get("available", 0))
        if zar >= 10:
            spend = zar * 0.99
            print(f"\n  BUY USDT with {spend:.2f} ZAR")
            if EXECUTE:
                st, d = await rest.request("POST", "/v1/orders/market",
                    {"pair": "USDTZAR", "side": "BUY", "quoteAmount": str(spend), "timeInForce": "IOC"},
                    subaccount_id=sub_id)
                print(f"    {st}")
            else:
                print("    [DRY RUN]")
            await asyncio.sleep(2)
            bals = {b["currency"]: b for b in await rest.balances(sub_id)}

    # Transfer all quote currencies to MAIN
    print(f"\n=== {sub_name}: transfer to MAIN ===")
    for cur in ["USDT", "USDC", "EURC", "ZAR"]:
        avail = float(bals.get(cur, {}).get("available", 0))
        if avail < 0.001:
            continue
        transfer_amt = avail * 0.99
        if transfer_amt < 0.001:
            continue
        print(f"  Transfer {transfer_amt:.8f} {cur}")
        if EXECUTE:
            st, d = await rest.request("POST", "/v1/account/subaccounts/transfer",
                {"fromId": sub_id, "toId": MAIN_ID, "currencyCode": cur,
                 "amount": str(transfer_amt), "allowBorrow": False})
            print(f"    {st}")
            await asyncio.sleep(0.2)
        else:
            print("    [DRY RUN]")

async def main():
    c = load()
    rest = ValrRest(api_key=c["MAIN_API_KEY"], api_secret=c["MAIN_API_SECRET"])

    mode = "LIVE" if EXECUTE else "DRY RUN"
    print(f"=== SWEEP DUST TO MAIN — {mode} ===")

    # ZAR subs
    for name in ["CMSZAR1", "CMSZAR2"]:
        await sell_and_transfer(rest, SUBS[name], name, ZAR_PAIRS, "ZAR")

    # USDT subs
    for name in ["CMSUSDT1", "CMSUSDT2"]:
        await sell_and_transfer(rest, SUBS[name], name, USDT_PAIRS, "USDT")

    # USDC subs — nothing to sell, just transfer
    for name in ["CMSUSDC1", "CMSUSDC2"]:
        bals = {b["currency"]: b for b in await rest.balances(SUBS[name])}
        print(f"\n=== {name}: transfer to MAIN ===")
        for cur in ["USDC", "EURC"]:
            avail = float(bals.get(cur, {}).get("available", 0))
            if avail < 0.001:
                continue
            print(f"  Transfer {avail * 0.99:.8f} {cur}")
            if EXECUTE:
                st, d = await rest.request("POST", "/v1/account/subaccounts/transfer",
                    {"fromId": SUBS[name], "toId": MAIN_ID, "currencyCode": cur,
                     "amount": str(avail * 0.99), "allowBorrow": False})
                print(f"    {st}")
                await asyncio.sleep(0.2)
            else:
                print("    [DRY RUN]")

    # Final MAIN
    if EXECUTE:
        await asyncio.sleep(2)
        st, data = await rest.request("GET", "/v1/account/balances")
        print(f"\n=== Final MAIN ===")
        if isinstance(data, list):
            for b in data:
                if float(b.get("available", 0)) > 0 and b["currency"] in ("USDT","USDC","ZAR","EURC"):
                    print(f"  {b['currency']}: {b['available']}")

    print(f"\n{'✅ DONE' if EXECUTE else '🟡 Dry run — add --execute'}")
    await rest.close()

if __name__ == "__main__":
    asyncio.run(main())
