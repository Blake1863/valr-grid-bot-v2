#!/usr/bin/env python3
"""Liquidate ALL base assets on all spot wash subs to USDT/USDC, transfer to MAIN.

Steps:
  1. Cancel all open orders on all subs
  2. CMSZAR subs: sell all base → ZAR, then buy USDT on USDTZAR
  3. CMSUSDT subs: sell all base → USDT
  4. CMSUSDC subs: sell EURC → USDC
  5. Transfer all USDT/USDC from all subs to MAIN
"""
import asyncio
import sys
import json
import time

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

# ZAR-paired assets
ZAR_PAIRS = {
    "BTC": "BTCZAR",
    "ETH": "ETHZAR",
    "SOL": "SOLZAR",
    "XRP": "XRPZAR",
    "AVAX": "AVAXZAR",
    "BNB": "BNBZAR",
    "LINK": "LINKZAR",
    "XAUT": "XAUTZAR",
}

USDT_PAIRS_BY_ASSET = {}

async def get_balances(rest, sub_id):
    bals = await rest.balances(sub_id)
    return {b["currency"]: b for b in bals}

async def cancel_all(rest, sub_id, sub_name):
    st, data = await rest.request("DELETE", "/v1/orders", subaccount_id=sub_id)
    print(f"  [{sub_name}] Cancel all orders: status={st}")
    if isinstance(data, list):
        print(f"    Cancelled {len(data)} orders")

async def get_market_summary(rest, pair):
    st, data = await rest.request("GET", f"/v1/public/{pair}/marketsummary")
    if st == 200 and isinstance(data, dict):
        return data
    return None

async def market_order(rest, sub_id, pair, side, *, base_amount=None, quote_amount=None, label=""):
    if not EXECUTE:
        if base_amount:
            print(f"  {side} {base_amount} {pair} {label} [DRY RUN]")
        elif quote_amount:
            print(f"  {side} {pair} quoteAmount={quote_amount} {label} [DRY RUN]")
        return 200, {}
    body = {"pair": pair, "side": side.upper(), "timeInForce": "IOC"}
    if base_amount is not None:
        body["baseAmount"] = str(base_amount)
    if quote_amount is not None:
        body["quoteAmount"] = str(quote_amount)
    st, data = await rest.request("POST", "/v1/orders/market", body, subaccount_id=sub_id)
    if isinstance(data, dict):
        print(f"    {side} {pair}: status={st} fills={data.get('fills',[])}")
    else:
        print(f"    {side} {pair}: status={st} data={data}")
    await asyncio.sleep(0.5)
    return st, data

async def transfer_to_main(rest, sub_id, currency, amount, sub_name):
    amt = float(amount)
    if amt < 0.001:
        return
    transfer_amt = amt * 0.99 if amt > 0.01 else amt
    if transfer_amt < 0.001:
        return
    print(f"  Transfer {transfer_amt:.8f} {currency} {sub_name} → MAIN")
    if not EXECUTE:
        print("    [DRY RUN]")
        return
    body = {
        "fromId": sub_id,
        "toId": MAIN_ID,
        "currencyCode": currency,
        "amount": str(transfer_amt),
        "allowBorrow": False,
    }
    st, data = await rest.request("POST", "/v1/account/subaccounts/transfer", body)
    print(f"    status={st}")
    await asyncio.sleep(0.3)

async def main():
    c = load()
    rest = ValrRest(api_key=c["MAIN_API_KEY"], api_secret=c["MAIN_API_SECRET"])

    mode = "🔴 LIVE" if EXECUTE else "🟡 DRY RUN"
    print(f"=== LIQUIDATE ALL SPOT WASH SUBS → USDT → MAIN — {mode} ===")
    if not EXECUTE:
        print("Add --execute to actually perform actions\n")

    # ---- Step 0: Current balances ----
    print("\n=== Current Balances ===")
    balances = {}
    for name, sub_id in SUBS.items():
        b = await get_balances(rest, sub_id)
        balances[name] = b
        non_zero = {cur: bal["available"] for cur, bal in b.items() if float(bal.get("available", 0)) > 0}
        print(f"  {name}: {non_zero}")

    # ---- Step 1: Cancel all open orders ----
    print("\n=== Step 1: Cancel all open orders ===")
    for name, sub_id in SUBS.items():
        await cancel_all(rest, sub_id, name)
    await asyncio.sleep(2)

    # ---- Step 2: Check USDT pairs ----
    print("\n=== Step 2: Available USDT pairs ===")
    usdt_assets = ["JUP", "TRUMP", "HOODX", "SPYX", "VALR10", "TSLAX", "CRCLX",
                   "COINX", "MSTRX", "BITGOLD", "NVDAX", "PUMP", "XAUT"]
    for asset in usdt_assets:
        pair = f"{asset}USDT"
        summary = await get_market_summary(rest, pair)
        if summary:
            USDT_PAIRS_BY_ASSET[asset] = pair
            print(f"  ✅ {pair}")
        else:
            print(f"  ❌ {pair} not found/inactive")
    print(f"  {len(USDT_PAIRS_BY_ASSET)} USDT pairs available")

    # ---- Step 3: Sell ZAR-sub base assets → ZAR ----
    print("\n=== Step 3: Sell ZAR-sub base assets → ZAR ===")
    for name in ["CMSZAR1", "CMSZAR2"]:
        sub_id = SUBS[name]
        b = balances[name]
        for asset, pair in ZAR_PAIRS.items():
            avail = float(b.get(asset, {}).get("available", 0))
            if avail > 0:
                sell_amt = avail * 0.98
                await market_order(rest, sub_id, pair, "SELL", base_amount=sell_amt, label=f"[{name}]")

    # Wait for fills
    await asyncio.sleep(5)

    # ---- Step 4: Refresh ZAR balances, buy USDT ----
    print("\n=== Step 4: Refresh ZAR balances ===")
    for name in ["CMSZAR1", "CMSZAR2"]:
        sub_id = SUBS[name]
        b = await get_balances(rest, sub_id)
        balances[name] = b
        zar = float(b.get("ZAR", {}).get("available", 0))
        print(f"  {name}: ZAR={zar}")

    print("\n=== Step 5: Buy USDT with ZAR ===")
    for name in ["CMSZAR1", "CMSZAR2"]:
        sub_id = SUBS[name]
        zar_avail = float(balances[name].get("ZAR", {}).get("available", 0))
        if zar_avail >= 10:
            spend = zar_avail * 0.98
            await market_order(rest, sub_id, "USDTZAR", "BUY", quote_amount=spend, label=f"[{name}]")

    # ---- Step 6: Sell USDT-sub base assets → USDT ----
    await asyncio.sleep(3)
    print("\n=== Step 6: Sell USDT-sub base assets → USDT ===")
    for name in ["CMSUSDT1", "CMSUSDT2"]:
        sub_id = SUBS[name]
        b = await get_balances(rest, sub_id)
        balances[name] = b
        for asset, pair in USDT_PAIRS_BY_ASSET.items():
            avail = float(b.get(asset, {}).get("available", 0))
            if avail > 0:
                sell_amt = avail * 0.98
                await market_order(rest, sub_id, pair, "SELL", base_amount=sell_amt, label=f"[{name}]")

    # ---- Step 7: Sell EURC → USDC on USDC subs ----
    await asyncio.sleep(3)
    print("\n=== Step 7: Sell EURC → USDC ===")
    for name in ["CMSUSDC1", "CMSUSDC2"]:
        sub_id = SUBS[name]
        b = await get_balances(rest, sub_id)
        balances[name] = b
        eurc = float(b.get("EURC", {}).get("available", 0))
        if eurc > 0.01:
            sell_amt = eurc * 0.98
            await market_order(rest, sub_id, "EURCUSDC", "SELL", base_amount=sell_amt, label=f"[{name}]")

    # ---- Step 8: Final balance snapshot ----
    await asyncio.sleep(3)
    print("\n=== Step 8: Final Balances (pre-transfer) ===")
    quote_currencies = {"USDT", "USDC", "EURC", "ZAR"}
    total_usdt_equiv = 0
    for name, sub_id in SUBS.items():
        b = await get_balances(rest, sub_id)
        balances[name] = b
        quote_bals = {cur: b[cur]["available"] for cur in quote_currencies if cur in b and float(b[cur].get("available", 0)) > 0}
        base_left = {cur: b[cur]["available"] for cur, bal in b.items()
                     if float(bal.get("available", 0)) > 0 and cur not in quote_currencies}
        print(f"  {name}: quote={quote_bals} base_left={base_left if base_left else 'none'}")

    # ---- Step 9: Transfer all quote currencies to MAIN ----
    await asyncio.sleep(2)
    print("\n=== Step 9: Transfer all quote currencies to MAIN ===")
    for name, sub_id in SUBS.items():
        for cur in ["USDT", "USDC", "EURC", "ZAR"]:
            avail = balances[name].get(cur, {}).get("available", "0")
            await transfer_to_main(rest, sub_id, cur, avail, name)

    # ---- Step 10: Final MAIN balance ----
    await asyncio.sleep(2)
    print("\n=== Final MAIN Balance ===")
    # Primary account: call without subaccount header
    st, main_data = await rest.request("GET", "/v1/account/balances")
    if isinstance(main_data, list):
        for b in main_data:
            avail = float(b.get("available", 0))
            if avail > 0:
                print(f"  {b['currency']}: {b['available']}")
    else:
        print(f"  Error fetching MAIN: {st} {main_data}")

    print(f"\n{'✅ DONE' if EXECUTE else '🟡 Dry run complete — add --execute to run for real'}")
    await rest.close()

if __name__ == "__main__":
    asyncio.run(main())
