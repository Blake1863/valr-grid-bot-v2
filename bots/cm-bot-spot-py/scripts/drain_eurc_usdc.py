#!/usr/bin/env python3
"""Drain excess EURC and USDC from CMS1/CMS2 → USDT.

Path: EURC → USDC → ZAR → USDT  |  USDC → ZAR → USDT

Reserve targets (per sub, enough for ~15 EURCUSDC cycles):
  EURC ≥ 15.00   (≈ $17.70 at 1.177)
  USDC ≥ 25.00   (≈ $25.00)

Only converts amounts ABOVE the reserve.
"""
import asyncio
import sys
import uuid

sys.path.insert(0, "src")
from creds import load
from valr_rest import ValrRest

# Reserves per sub (EURCUSDC pair needs both currencies)
RESERVE_EURC = 15.0   # ≈ $17.70
RESERVE_USDC = 25.0   # $25


def load_creds():
    c = load()
    return {
        "api_key": c["MAIN_API_KEY"],
        "api_secret": c["MAIN_API_SECRET"],
        "cms1_id": c["CM1_SUBACCOUNT_ID"],
        "cms2_id": c["CM2_SUBACCOUNT_ID"],
    }


async def get_balances(rest, sub_id):
    bals = await rest.balances(sub_id)
    return {b["currency"]: b for b in bals}


async def market_order(rest, sub_id, pair, side, *, quote_amount=None, base_amount=None):
    body = {"pair": pair, "side": side.upper(), "timeInForce": "IOC"}
    if quote_amount is not None:
        body["quoteAmount"] = str(quote_amount)
    if base_amount is not None:
        body["baseAmount"] = str(base_amount)
    body["customerOrderId"] = f"drain-{uuid.uuid4().hex[:12]}"
    return await rest.request("POST", "/v1/orders/market", body, subaccount_id=sub_id)


async def drain_sub(rest, name, sub_id):
    bals = await get_balances(rest, sub_id)

    eurc_avail = float(bals.get("EURC", {}).get("available", 0))
    usdc_avail = float(bals.get("USDC", {}).get("available", 0))

    excess_eurc = max(0, eurc_avail - RESERVE_EURC)
    excess_usdc = max(0, usdc_avail - RESERVE_USDC)

    if excess_eurc < 1.0 and excess_usdc < 1.0:
        # Nothing worth draining — print concise summary
        print(f"[{name}] EURC={eurc_avail:.2f}/{RESERVE_EURC:.0f}  USDC={usdc_avail:.2f}/{RESERVE_USDC:.0f}  (ok)")
        return

    print(f"[{name}] EURC={eurc_avail:.2f}/{RESERVE_EURC:.0f}  USDC={usdc_avail:.2f}/{RESERVE_USDC:.0f}")

    if excess_eurc >= 1.0:
        sell_qty = excess_eurc * 0.98
        st, data = await market_order(rest, sub_id, "EURCUSDC", "SELL", base_amount=sell_qty)
        if st not in (200, 201, 202):
            print(f"  [{name}] EURC→USDC sell failed: {st} {data}")
            return
        print(f"  [{name}] SELL {sell_qty:.2f} EURC → USDC")
        await asyncio.sleep(1)

    # Re-read USDC balance
    bals2 = await get_balances(rest, sub_id)
    usdc_avail2 = float(bals2.get("USDC", {}).get("available", 0))
    excess_usdc2 = max(0, usdc_avail2 - RESERVE_USDC)

    if excess_usdc2 < 5.0:
        return

    sell_usdc = excess_usdc2 * 0.98
    st, data = await market_order(rest, sub_id, "USDCZAR", "SELL", base_amount=sell_usdc)
    if st not in (200, 201, 202):
        print(f"  [{name}] USDC→ZAR sell failed: {st} {data}")
        return
    print(f"  [{name}] SELL {sell_usdc:.2f} USDC → ZAR")
    await asyncio.sleep(1)

    # Re-read ZAR balance
    bals3 = await get_balances(rest, sub_id)
    zar_avail = float(bals3.get("ZAR", {}).get("available", 0))
    if zar_avail < 10:
        return

    spend_zar = zar_avail * 0.98
    st, data = await market_order(rest, sub_id, "USDTZAR", "BUY", quote_amount=spend_zar)
    if st not in (200, 201, 202):
        print(f"  [{name}] ZAR→USDT buy failed: {st} {data}")
        return
    print(f"  [{name}] BUY USDT with {spend_zar:.2f} ZAR")


async def main():
    creds = load_creds()
    rest = ValrRest(api_key=creds["api_key"], api_secret=creds["api_secret"])

    for name, sub_id in [("CMS1", creds["cms1_id"]), ("CMS2", creds["cms2_id"])]:
        await drain_sub(rest, name, sub_id)

    await rest.close()


if __name__ == "__main__":
    asyncio.run(main())
