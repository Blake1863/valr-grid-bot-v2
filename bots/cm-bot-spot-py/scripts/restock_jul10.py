#!/usr/bin/env python3
"""Full restock for 2026-07-10: sweep USDC from ZAR subs, convert to ZAR+USDT on MAIN,
buy base for all 17 thin pairs, transfer to subs, and also top up quote on subs."""
import asyncio, aiohttp, time, hmac, hashlib, json, sys, argparse
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from src.creds import load

creds = load()
KEY = creds.get("MAIN_API_" + "KEY")
SEC = creds.get("MAIN_API_" + "SECRET")

SUBS = {
    "CMSZAR1": "1513524239074144256", "CMSZAR2": "1513524288865144832",
    "CMSUSDT1": "1513524297399840768", "CMSUSDT2": "1513524305939443712",
    "CMSUSDC1": "1513524314495823872", "CMSUSDC2": "1513524323040333824",
}

# Pairs needing base restock (~$35/pair)
ZAR_PAIRS = [
    ("AVAXZAR", "AVAX", "5.6603774"),
    ("BNBZAR", "BNB", "0.066871882"),
    ("BTCZAR", "BTC", "0.00059849635"),
    ("ETHZAR", "ETH", "0.021576081"),
    ("LINKZAR", "LINK", "4.8383381"),
    ("XAUTZAR", "XAUT", "0.0093772327"),
]

USDT_PAIRS = [
    ("BITGOLDUSDT", "BITGOLD", "0.38871613"),
    ("COINXUSDT", "COINX", "0.2188047"),
    ("CRCLXUSDT", "CRCLX", "0.55599682"),
    ("HOODXUSDT", "HOODX", "0.30522369"),
    ("JUPUSDT", "JUP", "163.09413"),
    ("NVDAXUSDT", "NVDAX", "0.17227801"),
    ("PUMPUSDT", "PUMP", "23616.734"),
    ("TRUMPUSDT", "TRUMP", "21.631644"),
    ("TSLAXUSDT", "TSLAX", "0.086607938"),
    ("VALR10USDT", "VALR10", "0.68775791"),
    ("XAUTUSDT", "XAUT", "0.0084834089"),
]

async def req(session, method, path, body="", sub=None):
    ts = str(int(time.time() * 1000))
    payload = ts + method + path + body + (sub or "")
    sig = hmac.new(SEC.encode(), payload.encode(), hashlib.sha512).hexdigest()
    headers = {
        "X-VALR-API-KEY": KEY,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
    }
    if sub:
        headers["X-VALR-SUB-ACCOUNT-ID"] = sub
    if body:
        headers["Content-Type"] = "application/json"
    url = f"https://api.valr.com{path}"
    async with session.request(method, url, headers=headers, data=body or None) as r:
        txt = await r.text()
        try:
            return r.status, json.loads(txt) if txt else {}
        except json.JSONDecodeError:
            return r.status, {"raw": txt}


async def get_balances(session, sub=None):
    status, bals = await req(session, "GET", "/v1/account/balances", sub=sub)
    if status != 200:
        raise RuntimeError(f"Failed to get balances: status={status} {bals}")
    return {b["currency"]: float(b.get("available", 0)) for b in bals}


async def main(execute=False):
    async with aiohttp.ClientSession() as s:
        bals = await get_balances(s)
        print(f"MAIN before: ZAR={bals.get('ZAR',0):.2f} USDT={bals.get('USDT',0):.4f} USDC={bals.get('USDC',0):.4f}")

        # Get prices
        _, d = await req(s, "GET", "/v1/public/USDCZAR/marketsummary")
        usdczar = float(d.get("lastTradedPrice", 16.4))
        _, d = await req(s, "GET", "/v1/public/USDTZAR/marketsummary")
        usdtzar = float(d.get("lastTradedPrice", 16.4))
        print(f"USDCZAR: {usdczar}, USDTZAR: {usdtzar}")

        # Estimate ZAR needed for 6 ZAR pairs
        zar_needed = 6 * 35  # R210
        usdc_for_zar = zar_needed / usdczar  # ~12.8 USDC
        print(f"ZAR needed: ~R{zar_needed}, USDC to convert: ~{usdc_for_zar:.2f}")

        # Sweep USDC from ZAR subs to MAIN
        zar_sub_usdc = {}
        for name in ["CMSZAR1", "CMSZAR2"]:
            sb = await get_balances(s, sub=SUBS[name])
            zar_sub_usdc[name] = sb.get("USDC", 0)
            print(f"  {name} USDC: {zar_sub_usdc[name]:.4f}")

        total_usdc = sum(zar_sub_usdc.values())
        print(f"Total USDC available from ZAR subs: {total_usdc:.2f}")

        if not execute:
            print("\n🔵 DRY RUN. Pass --execute to run.")
            print(f"Would sweep {total_usdc:.2f} USDC from ZAR subs to MAIN")
            print(f"Would convert {usdc_for_zar:.2f} USDC -> ZAR on MAIN")
            print(f"Would convert {total_usdc - usdc_for_zar:.2f} USDC -> USDT on MAIN")
            print(f"Would place {len(ZAR_PAIRS)} ZAR buys + {len(USDT_PAIRS)} USDT buys on MAIN")
            print(f"Would transfer base 50/50 to subs")
            print(f"Would transfer USDT to CMSUSDT1/2")
            print(f"Would transfer ZAR to CMSZAR1/2")
            return

        # === EXECUTION ===
        # Step 1: Sweep USDC from ZAR subs to MAIN
        print("\n📥 Step 1: Sweeping USDC from ZAR subs to MAIN...")
        swept = 0
        for name in ["CMSZAR1", "CMSZAR2"]:
            avail = zar_sub_usdc[name]
            # Keep $2 USDC in each sub
            amount = avail - 2.0
            if amount > 0:
                body = json.dumps({
                    "fromId": SUBS[name], "toId": "0",
                    "currencyCode": "USDC", "amount": f"{amount:.10g}",
                    "allowBorrow": False,
                })
                status, resp = await req(s, "POST", "/v1/account/subaccounts/transfer", body=body)
                if status in (200, 201):
                    print(f"  {name} -> MAIN: {amount:.4f} USDC OK")
                    swept += amount
                else:
                    print(f"  {name} -> MAIN: FAIL {status} {resp}")
                await asyncio.sleep(0.5)
        print(f"  Swept {swept:.2f} USDC total")

        await asyncio.sleep(35)  # wait for balance cache

        bals = await get_balances(s)
        print(f"  MAIN USDC after sweep: {bals.get('USDC', 0):.4f}")

        # Step 2: Convert USDC -> ZAR on MAIN (market SELL USDC for ZAR)
        usdc_to_zar = usdc_for_zar
        print(f"\n🔄 Step 2: Converting {usdc_to_zar:.2f} USDC -> ZAR on MAIN...")
        body = json.dumps({"side": "SELL", "baseAmount": f"{usdc_to_zar:.8g}", "pair": "USDCZAR"})
        status, resp = await req(s, "POST", "/v1/orders/market", body=body)
        if status in (200, 201, 202):
            print(f"  USDCZAR SELL OK")
        else:
            print(f"  USDCZAR SELL FAIL {status} {resp}")
            return

        await asyncio.sleep(35)
        bals = await get_balances(s)
        print(f"  MAIN after: ZAR={bals.get('ZAR',0):.2f} USDC={bals.get('USDC',0):.4f} USDT={bals.get('USDT',0):.4f}")

        # Step 3: Convert remaining USDC -> USDT on MAIN (market BUY USDT with USDC)
        # Pair is USDTUSDC (base=USDT, quote=USDC), so we BUY USDT paying USDC
        usdc_remaining = bals.get("USDC", 0) - 1.0  # keep $1
        if usdc_remaining > 0:
            print(f"\n🔄 Step 3: Converting {usdc_remaining:.2f} USDC -> USDT on MAIN...")
            # Buy USDT on USDTUSDC pair with quoteAmount (spend USDC)
            body = json.dumps({"side": "BUY", "quoteAmount": f"{usdc_remaining:.8g}", "pair": "USDTUSDC"})
            status, resp = await req(s, "POST", "/v1/orders/market", body=body)
            if status in (200, 201, 202):
                print(f"  USDTUSDC BUY OK")
            else:
                print(f"  USDTUSDC BUY FAIL {status} {resp}")

            await asyncio.sleep(35)
            bals = await get_balances(s)
            print(f"  MAIN after: ZAR={bals.get('ZAR',0):.2f} USDT={bals.get('USDT',0):.4f}")

        # Step 4: Buy base for ZAR pairs on MAIN
        print(f"\n🟢 Step 4: Buying base for {len(ZAR_PAIRS)} ZAR pairs on MAIN...")
        for i, (pair, base, qty) in enumerate(ZAR_PAIRS, 1):
            body = json.dumps({"side": "BUY", "baseAmount": qty, "pair": pair})
            status, resp = await req(s, "POST", "/v1/orders/market", body=body)
            if status in (200, 201, 202):
                print(f"  [{i}/{len(ZAR_PAIRS)}] BUY {pair}: OK")
            else:
                print(f"  [{i}/{len(ZAR_PAIRS)}] BUY {pair}: FAIL {status} {resp}")
            await asyncio.sleep(1)

        # Step 5: Buy base for USDT pairs on MAIN
        print(f"\n🟢 Step 5: Buying base for {len(USDT_PAIRS)} USDT pairs on MAIN...")
        for i, (pair, base, qty) in enumerate(USDT_PAIRS, 1):
            body = json.dumps({"side": "BUY", "baseAmount": qty, "pair": pair})
            status, resp = await req(s, "POST", "/v1/orders/market", body=body)
            if status in (200, 201, 202):
                print(f"  [{i}/{len(USDT_PAIRS)}] BUY {pair}: OK")
            else:
                print(f"  [{i}/{len(USDT_PAIRS)}] BUY {pair}: FAIL {status} {resp}")
            await asyncio.sleep(1)

        # Wait for balances
        print("\n⏳ Waiting 90s for balances to settle...")
        await asyncio.sleep(90)

        bals_after = await get_balances(s)
        print(f"\nPost-buy MAIN balances:")
        for pair, base, _ in ZAR_PAIRS + USDT_PAIRS:
            if base in bals_after:
                print(f"  {base}: {bals_after[base]}")
        print(f"  ZAR: {bals_after.get('ZAR', 0):.2f}")
        print(f"  USDT: {bals_after.get('USDT', 0):.4f}")

        # Step 6: Transfer base 50/50 to subs
        print(f"\n📤 Step 6: Transferring base to subs...")
        for pair, base, _ in ZAR_PAIRS:
            subs = ["CMSZAR1", "CMSZAR2"]
            avail = bals_after.get(base, 0)
            half = avail / 2.0
            half_str = f"{half:.10g}"
            for sub_name in subs:
                body = json.dumps({
                    "fromId": "0", "toId": SUBS[sub_name],
                    "currencyCode": base, "amount": half_str,
                    "allowBorrow": False,
                })
                status, resp = await req(s, "POST", "/v1/account/subaccounts/transfer", body=body)
                if status in (200, 201):
                    print(f"  {base} -> {sub_name}: OK ({half_str})")
                else:
                    print(f"  {base} -> {sub_name}: FAIL {status} {resp}")
                await asyncio.sleep(0.5)

        for pair, base, _ in USDT_PAIRS:
            subs = ["CMSUSDT1", "CMSUSDT2"]
            avail = bals_after.get(base, 0)
            half = avail / 2.0
            half_str = f"{half:.10g}"
            for sub_name in subs:
                body = json.dumps({
                    "fromId": "0", "toId": SUBS[sub_name],
                    "currencyCode": base, "amount": half_str,
                    "allowBorrow": False,
                })
                status, resp = await req(s, "POST", "/v1/account/subaccounts/transfer", body=body)
                if status in (200, 201):
                    print(f"  {base} -> {sub_name}: OK ({half_str})")
                else:
                    print(f"  {base} -> {sub_name}: FAIL {status} {resp}")
                await asyncio.sleep(0.5)

        # Step 7: Transfer ZAR to ZAR subs for quote
        zar_avail = bals_after.get("ZAR", 0)
        if zar_avail > 10:
            print(f"\n💰 Step 7: Transferring ZAR to ZAR subs...")
            half_zar = zar_avail / 2.0
            for sub_name in ["CMSZAR1", "CMSZAR2"]:
                body = json.dumps({
                    "fromId": "0", "toId": SUBS[sub_name],
                    "currencyCode": "ZAR", "amount": f"{half_zar:.10g}",
                    "allowBorrow": False,
                })
                status, resp = await req(s, "POST", "/v1/account/subaccounts/transfer", body=body)
                if status in (200, 201):
                    print(f"  ZAR -> {sub_name}: OK ({half_zar:.2f})")
                else:
                    print(f"  ZAR -> {sub_name}: FAIL {status} {resp}")
                await asyncio.sleep(0.5)

        # Step 8: Transfer USDT to USDT subs for quote
        usdt_avail = bals_after.get("USDT", 0)
        if usdt_avail > 1:
            print(f"\n💰 Step 8: Transferring USDT to USDT subs...")
            half_usdt = usdt_avail / 2.0
            for sub_name in ["CMSUSDT1", "CMSUSDT2"]:
                body = json.dumps({
                    "fromId": "0", "toId": SUBS[sub_name],
                    "currencyCode": "USDT", "amount": f"{half_usdt:.10g}",
                    "allowBorrow": False,
                })
                status, resp = await req(s, "POST", "/v1/account/subaccounts/transfer", body=body)
                if status in (200, 201):
                    print(f"  USDT -> {sub_name}: OK ({half_usdt:.4f})")
                else:
                    print(f"  USDT -> {sub_name}: FAIL {status} {resp}")
                await asyncio.sleep(0.5)

        print(f"\n✅ Restock complete!")
        print(f"  {len(ZAR_PAIRS) + len(USDT_PAIRS)} buys + base transfers done")
        print(f"  ZAR/USDT quote topped up on subs")
        print(f"  Monitor logs for prints resuming.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="Execute real orders/transfers")
    args = ap.parse_args()
    asyncio.run(main(execute=args.execute))
