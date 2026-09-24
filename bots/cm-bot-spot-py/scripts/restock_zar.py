#!/usr/bin/env python3
"""Restock ZAR subs — sell USDT→ZAR on main, buy base, transfer to subs."""
import argparse, asyncio, aiohttp, time, hmac, hashlib, json, sys
from decimal import Decimal, ROUND_DOWN
sys.path.insert(0, "src")
from creds import load

creds = load()
KEY = creds["MAIN_API_" + "KEY"]
SEC = creds["MAIN_API_" + "SECRET"]

MAIN_ID = "0"
ZAR_SUBS = {
    "CMSZAR1": "1513524239074144256",
    "CMSZAR2": "1513524288865144832",
}

ZAR_PAIRS = ["BTCZAR", "ETHZAR", "XRPZAR", "SOLZAR", "AVAXZAR", "BNBZAR", "LINKZAR", "XAUTZAR"]
BASE_MAP = {
    "BTCZAR": "BTC", "ETHZAR": "ETH", "XRPZAR": "XRP", "SOLZAR": "SOL",
    "AVAXZAR": "AVAX", "BNBZAR": "BNB", "LINKZAR": "LINK", "XAUTZAR": "XAUT",
}

async def _req(verb, path, body=None, sub=None, session=None):
    ts = str(int(time.time() * 1000))
    raw = json.dumps(body) if body else ""
    payload = ts + verb + path + raw
    if sub:
        payload += sub
    sig = hmac.new(SEC.encode(), payload.encode(), hashlib.sha512).hexdigest()
    headers = {
        "X-VALR-API-KEY": KEY,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }
    if sub:
        headers["X-VALR-SUB-ACCOUNT-ID"] = sub
    url = "https://api.valr.com" + path
    async with session.request(verb, url, headers=headers, json=body if body else None) as r:
        return await r.json()

async def get_balances(sub=None, session=None):
    data = await _req("GET", "/v1/account/balances", sub=sub, session=session)
    if isinstance(data, list):
        return {b["currency"]: Decimal(b["available"] or 0) for b in data}
    print(f"  BAL ERROR: {json.dumps(data)[:300]}")
    return {}

async def market_order(side, pair, quote_amount=None, base_amount=None, sub=None, session=None):
    body = {"side": side, "pair": pair}
    if quote_amount:
        body["quoteAmount"] = str(quote_amount)
    if base_amount:
        body["baseAmount"] = str(base_amount)
    result = await _req("POST", "/v1/orders/market", body=body, sub=sub, session=session)
    return result

async def transfer(from_id, to_id, currency, amount, session=None):
    body = {"fromId": str(from_id), "toId": str(to_id), "currencyCode": currency,
            "amount": str(amount), "allowBorrow": False}
    result = await _req("POST", "/v1/account/subaccounts/transfer", body=body, session=session)
    return result

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Actually do it")
    args = parser.parse_args()

    if not args.execute:
        print("DRY RUN — pass --execute to act")

    async with aiohttp.ClientSession() as s:

        # 1. Current balances
        print("\n=== CURRENT STATE ===")
        main_bal = await get_balances(session=s)
        zar_avail = main_bal.get("ZAR", Decimal(0))
        usdt_avail = main_bal.get("USDT", Decimal(0))
        print(f"MAIN: ZAR={zar_avail:.2f}, USDT={usdt_avail:.2f}")
        for sname, sid in ZAR_SUBS.items():
            sub_bal = await get_balances(sub=sid, session=s)
            print(f"  {sname}: ZAR={sub_bal.get('ZAR', Decimal(0)):.2f}")

        # 2. Calculate USDT to sell
        # Need R2,500 per sub = R5,000 total. Also need ~R40/pair × 8 = R320 for buys.
        # Total needed ~R5,320. Have zar_avail on main.
        target_zar = Decimal("5400")
        zar_short = max(Decimal(0), target_zar - zar_avail)

        if zar_short > 0:
            usdt_needed = (zar_short / Decimal("16.6")).quantize(Decimal("0.01"))
            usdt_to_sell = min(usdt_needed, usdt_avail)
            if usdt_to_sell < Decimal("10"):
                print(f"\n⚠️ Not enough USDT to sell (need ~{usdt_needed}, have {usdt_avail})")
                usdt_to_sell = usdt_avail
            zar_est = usdt_to_sell * Decimal("16.6")
            print(f"\n=== STEP 1: Sell ${usdt_to_sell:.2f} USDT → ~R{zar_est:.2f} ===")
            if args.execute:
                result = await market_order("SELL", "USDTZAR", quote_amount=str(usdt_to_sell), session=s)
                print(f"  Result: {json.dumps(result)[:300]}")
                print("  Waiting 10s for settle...")
                await asyncio.sleep(10)
        else:
            print(f"\n=== STEP 1: Already have enough ZAR (R{zar_avail:.2f}) ===")

        # 3. Re-check ZAR
        main_bal = await get_balances(session=s)
        zar_now = main_bal.get("ZAR", Decimal(0))
        print(f"\n=== STEP 2: ZAR on MAIN now R{zar_now:.2f} ===")

        # 4. Buy base pairs on MAIN
        spend_per_pair = (zar_now / len(ZAR_PAIRS)).quantize(Decimal("0.01"))
        # Keep R200 on main as buffer
        spend_budget = zar_now - Decimal("200")
        spend_per_pair = (spend_budget / len(ZAR_PAIRS)).quantize(Decimal("0.01"))
        if spend_per_pair < Decimal("10"):
            spend_per_pair = Decimal("50")
        print(f"Buying {len(ZAR_PAIRS)} pairs @ R{spend_per_pair:.2f} each")
        if args.execute:
            for pair in ZAR_PAIRS:
                result = await market_order("BUY", pair, quote_amount=str(spend_per_pair), session=s)
                print(f"  {pair}: {json.dumps(result)[:200]}")
                await asyncio.sleep(0.5)
            print("  Waiting 10s for fills...")
            await asyncio.sleep(10)

        # 5. Get base balances after buys
        main_bal = await get_balances(session=s)
        zar_final = main_bal.get("ZAR", Decimal(0))

        # 6. Transfer ZAR to subs (split evenly, keep ~R50 on main)
        zar_for_subs = zar_final - Decimal("50")
        zar_per_sub = (zar_for_subs / 2).quantize(Decimal("0.01"))
        print(f"\n=== STEP 3: Transfer R{zar_per_sub} ZAR to each sub ===")
        if args.execute:
            for sname, sid in ZAR_SUBS.items():
                result = await transfer(0, sid, "ZAR", str(zar_per_sub), session=s)
                print(f"  {sname}: {json.dumps(result)[:200]}")

        # 7. Transfer base to subs (50/50, leave tiny ulp on main)
        print(f"\n=== STEP 4: Transfer base to subs ===")
        if args.execute:
            main_bal = await get_balances(session=s)
            for pair in ZAR_PAIRS:
                base = BASE_MAP[pair]
                avail = main_bal.get(base, Decimal(0))
                if avail > Decimal("0.000001"):
                    # Leave a tiny amount on main to avoid rounding rejects
                    to_send = (avail - Decimal("0.00000001")).quantize(Decimal("0.00000001"))
                    half = (to_send / 2).quantize(Decimal("0.00000001"))
                    if half > Decimal("0.00000001"):
                        for sname, sid in ZAR_SUBS.items():
                            result = await transfer(0, sid, base, str(half), session=s)
                            print(f"  {sname}: {half} {base} → {json.dumps(result)[:150]}")
                        await asyncio.sleep(0.3)
                    else:
                        print(f"  {base}: too small to split ({avail})")
                else:
                    print(f"  {base}: nothing to transfer")

        # 8. Final state
        print(f"\n=== FINAL STATE ===")
        main_bal = await get_balances(session=s)
        print(f"MAIN: {', '.join(f'{k}={v:.6f}' for k,v in sorted(main_bal.items()) if v > Decimal('0.0001'))}")
        for sname, sid in ZAR_SUBS.items():
            sub_bal = await get_balances(sub=sid, session=s)
            print(f"  {sname}: ZAR={sub_bal.get('ZAR', Decimal(0)):.2f}")

        print("\nDone!")

if __name__ == "__main__":
    asyncio.run(main())
