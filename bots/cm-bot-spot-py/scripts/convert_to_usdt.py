#!/usr/bin/env python3
"""Convert EURC and USDC on CMS1/CMS2 to USDT.

Path:
  1. EURC → USDC: SELL EURC on EURCUSDC (market)
  2. USDC → ZAR:  SELL USDC on USDCZAR (market)
  3. ZAR → USDT:  BUY USDT on USDTZAR (market)
"""
import asyncio
import sys
import time
import uuid
import json

sys.path.insert(0, "src")
from creds import load
from valr_rest import ValrRest


def load_creds():
    c = load()
    return {
        "api_key": c["MAIN_API_KEY"],
        "api_secret": c["MAIN_API_SECRET"],
        "cms1_id": c["CM1_SUBACCOUNT_ID"],
        "cms2_id": c["CM2_SUBACCOUNT_ID"],
    }


async def get_balances(rest, sub_id):
    """Return dict {currency: {avail, reserved, total}}."""
    bals = await rest.balances(sub_id)
    return {b["currency"]: b for b in bals}


async def market_order(
    rest, sub_id, pair, side, *, quote_amount=None, base_amount=None
):
    """Place a market order. Returns (status, data)."""
    body = {"pair": pair, "side": side.upper(), "timeInForce": "IOC"}
    if quote_amount is not None:
        body["quoteAmount"] = str(quote_amount)
    if base_amount is not None:
        body["baseAmount"] = str(base_amount)
    cid = f"conv-{uuid.uuid4().hex[:12]}"
    body["customerOrderId"] = cid
    st, data = await rest.request("POST", "/v1/orders/market", body, subaccount_id=sub_id)
    print(f"  Market {side} {pair}: status={st} data={json.dumps(data, indent=2) if isinstance(data, (dict, list)) else data}")
    return st, data


async def subaccount_transfer(rest, from_id, to_id, currency, amount):
    """Transfer between subaccounts."""
    st, data = await rest.subaccount_transfer(
        from_id=from_id, to_id=to_id, currency=currency, amount=str(amount)
    )
    print(f"  Transfer {currency} {amount}: {from_id} → {to_id}: status={st} data={data}")
    return st, data


async def main():
    creds = load_creds()
    rest = ValrRest(api_key=creds["api_key"], api_secret=creds["api_secret"])

    subs = {
        "CMS1": creds["cms1_id"],
        "CMS2": creds["cms2_id"],
    }

    # Step 0: Show current balances
    print("=== Current Balances ===")
    balances = {}
    for name, sub_id in subs.items():
        b = await get_balances(rest, sub_id)
        balances[name] = b
        eurc = b.get("EURC", {})
        usdc = b.get("USDC", {})
        usdt = b.get("USDT", {})
        print(f"  {name}: EURC={eurc.get('avail','0')} USDC={usdc.get('avail','0')} USDT={usdt.get('avail','0')}")

    # Step 1: Sell EURC → USDC on EURCUSDC
    print("\n=== Step 1: SELL EURC → USDC (EURCUSDC) ===")
    for name, sub_id in subs.items():
        eurc_bal = balances[name].get("EURC", {})
        eurc_avail = float(eurc_bal.get("available", 0))
        if eurc_avail < 0.01:
            print(f"  {name}: No EURC to sell")
            continue
        # Sell 95% to leave a tiny buffer
        sell_qty = eurc_avail * 0.95
        print(f"  {name}: SELL {sell_qty:.2f} EURC on EURCUSDC")
        st, data = await market_order(rest, sub_id, "EURCUSDC", "SELL", base_amount=sell_qty)

    # Small pause for fills to settle
    await asyncio.sleep(2)

    # Get updated USDC balances after EURC sells
    print("\n=== Updated balances after EURC sell ===")
    for name, sub_id in subs.items():
        b = await get_balances(rest, sub_id)
        balances[name] = b
        usdc = b.get("USDC", {})
        print(f"  {name}: USDC={usdc.get('avail','0')}")

    # Step 2: Sell USDC → ZAR on USDCZAR
    print("\n=== Step 2: SELL USDC → ZAR (USDCZAR) ===")
    for name, sub_id in subs.items():
        usdc_bal = balances[name].get("USDC", {})
        usdc_avail = float(usdc_bal.get("available", 0))
        if usdc_avail < 0.01:
            print(f"  {name}: No USDC to sell")
            continue
        # Sell 95% to leave tiny buffer
        sell_qty = usdc_avail * 0.95
        print(f"  {name}: SELL {sell_qty:.2f} USDC on USDCZAR")
        st, data = await market_order(rest, sub_id, "USDCZAR", "SELL", base_amount=sell_qty)

    await asyncio.sleep(2)

    # Get updated ZAR balances
    print("\n=== Updated balances after USDC sell ===")
    for name, sub_id in subs.items():
        b = await get_balances(rest, sub_id)
        balances[name] = b
        zar = b.get("ZAR", {})
        print(f"  {name}: ZAR={zar.get('avail','0')}")

    # Step 3: Buy USDT with ZAR on USDTZAR
    print("\n=== Step 3: BUY USDT with ZAR (USDTZAR) ===")
    for name, sub_id in subs.items():
        zar_bal = balances[name].get("ZAR", {})
        zar_avail = float(zar_bal.get("available", 0))
        if zar_avail < 1.0:
            print(f"  {name}: No ZAR to spend")
            continue
        # Spend 95% of ZAR to buy USDT
        spend_zar = zar_avail * 0.95
        print(f"  {name}: BUY USDT with {spend_zar:.2f} ZAR on USDTZAR")
        st, data = await market_order(rest, sub_id, "USDTZAR", "BUY", quote_amount=spend_zar)

    await asyncio.sleep(2)

    # Final balances
    print("\n=== Final Balances ===")
    for name, sub_id in subs.items():
        b = await get_balances(rest, sub_id)
        usdt = b.get("USDT", {})
        print(f"  {name}: USDT={usdt.get('avail','0')}")

    await rest.close()


if __name__ == "__main__":
    asyncio.run(main())
