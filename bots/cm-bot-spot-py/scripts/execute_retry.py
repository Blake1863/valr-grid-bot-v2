#!/usr/bin/env python3
"""Retry failed orders from execute_rebalance.py.

Plan:
  Phase A2 — retry 3 CMS2 sells with wider slippage (5 ticks)
  Then    — internal transfer USDT from CMS2 to CMS1 to fund the failed CMS1 buys
  Phase C2 — retry the 5 failed buys (3 CMS1 USDT + 2 ETHZAR)
"""
import asyncio
import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.creds import load
from src.valr_rest import ValrRest
from src.pair_meta import parse as parse_meta, format_price, format_qty, round_to_tick

SLIPPAGE_TICKS = 5  # more aggressive on retry


def cid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:14]}"


async def get_pair_metas(rest: ValrRest, pairs):
    st, all_pairs = await rest.request("GET", "/v1/public/pairs")
    by_symbol = {p["symbol"]: p for p in all_pairs if isinstance(p, dict)}
    return {sym: parse_meta(by_symbol[sym]) for sym in pairs}


async def get_book(rest: ValrRest, pair: str):
    st, ob = await rest.request("GET", f"/v1/public/{pair}/orderbook")
    asks = ob.get("Asks") or ob.get("asks") or []
    bids = ob.get("Bids") or ob.get("bids") or []
    return Decimal(asks[0]["price"]), Decimal(bids[0]["price"])


async def get_status(rest: ValrRest, sub_id: str, customer_order_id: str):
    st, data = await rest.order_history_by_cid(sub_id, customer_order_id)
    if isinstance(data, list) and data:
        latest = data[0] if isinstance(data[0], dict) else None
        if latest:
            return latest.get("orderStatusType"), latest.get("executedQuantity"), latest.get("executedPrice")
    return None, None, None


async def execute_one(rest, creds, action, metas, slippage_ticks=SLIPPAGE_TICKS):
    sub_label = action["sub"]
    sub_id = creds["CM1_SUBACCOUNT_ID"] if sub_label == "CMS1" else creds["CM2_SUBACCOUNT_ID"]
    pair = action["pair"]
    side = action["side"]
    qty_native = Decimal(str(action["qty_native"]))
    meta = metas[pair]

    ask, bid = await get_book(rest, pair)
    tick = meta.tick_size
    if side == "SELL":
        price = round_to_tick(bid - tick * slippage_ticks, tick)
    else:
        price = round_to_tick(ask + tick * slippage_ticks, tick)

    base_step = Decimal("1") / (Decimal("10") ** meta.base_decimals)
    qty_floored = (qty_native / base_step).to_integral_value(rounding="ROUND_DOWN") * base_step
    if qty_floored < meta.min_base_amount:
        qty_floored = meta.min_base_amount

    co_id = cid(f"rt-{side[0].lower()}")
    qty_str = format_qty(qty_floored, meta)
    price_str = format_price(price, meta)
    print(f"  → {sub_label} {side} {qty_str} {pair} @ {price_str} (cid={co_id})")
    st, data = await rest.place_limit_rest(
        sub_id, pair=pair, side=side, price=price_str, quantity=qty_str,
        post_only=False, customer_order_id=co_id, time_in_force="IOC",
    )
    if st not in (200, 201, 202):
        return False, f"placement failed st={st} resp={data}"
    await asyncio.sleep(0.4)
    for _ in range(8):
        status, exq, exp = await get_status(rest, sub_id, co_id)
        if status in ("Filled", "Partially Filled", "Cancelled", "Failed", "Expired"):
            return True, {"status": status, "executed_qty": exq, "executed_price": exp}
        await asyncio.sleep(0.5)
    return True, {"status": "unknown", "cid": co_id}


# Failed orders to retry (from inspecting the original results)
FAILED_SELLS_CMS2 = [
    {"sub": "CMS2", "currency": "XAUT", "pair": "XAUTUSDT", "side": "SELL",
     "qty_native": 0.0216, "usd_value": 98.07, "into": "USDT"},
    {"sub": "CMS2", "currency": "NVDAX", "pair": "NVDAXUSDT", "side": "SELL",
     "qty_native": 0.199, "usd_value": 39.02, "into": "USDT"},
    {"sub": "CMS2", "currency": "SPYX", "pair": "SPYXUSDT", "side": "SELL",
     "qty_native": 0.087, "usd_value": 62.91, "into": "USDT"},
]

FAILED_BUYS_CMS1 = [
    {"pair": "BITGOLDUSDT", "sub": "CMS1", "base": "BITGOLD", "quote": "USDT", "side": "BUY",
     "qty_native": 0.108361, "usd_value": 11.40},
    {"pair": "VALR10USDT", "sub": "CMS1", "base": "VALR10", "quote": "USDT", "side": "BUY",
     "qty_native": 0.185568, "usd_value": 11.58},
    {"pair": "PUMPUSDT", "sub": "CMS1", "base": "PUMP", "quote": "USDT", "side": "BUY",
     "qty_native": 6593.40, "usd_value": 12.00},
]

FAILED_BUYS_ETH = [
    {"pair": "ETHZAR", "sub": "CMS1", "base": "ETH", "quote": "ZAR", "side": "BUY",
     "qty_native": 0.00279115, "usd_value": 6.59},
    {"pair": "ETHZAR", "sub": "CMS2", "base": "ETH", "quote": "ZAR", "side": "BUY",
     "qty_native": 0.00308285, "usd_value": 7.27},
]


async def main():
    creds = load()
    all_pairs = sorted({a["pair"] for a in FAILED_SELLS_CMS2 + FAILED_BUYS_CMS1 + FAILED_BUYS_ETH})
    print(f"Loading metadata for {len(all_pairs)} pairs...")
    async with ValrRest(creds["MAIN_API_KEY"], creds["MAIN_API_SECRET"]) as rest:
        metas = await get_pair_metas(rest, all_pairs)

        # Phase A2: retry CMS2 sells (raises CMS2 USDT)
        print("\n========== PHASE A2: retry failed CMS2 sells ==========")
        for s in FAILED_SELLS_CMS2:
            ok, info = await execute_one(rest, creds, s, metas)
            print(f"     → {info}")
            await asyncio.sleep(0.3)

        await asyncio.sleep(2)
        # Check CMS2 USDT
        bals2 = await rest.balances(creds["CM2_SUBACCOUNT_ID"])
        cms2_usdt = next((float(b["available"]) for b in bals2 if b["currency"] == "USDT"), 0)
        bals1 = await rest.balances(creds["CM1_SUBACCOUNT_ID"])
        cms1_usdt = next((float(b["available"]) for b in bals1 if b["currency"] == "USDT"), 0)
        print(f"\n  After A2: CMS1 USDT={cms1_usdt:.4f}  CMS2 USDT={cms2_usdt:.4f}")

        # Internal transfer: CMS2 → CMS1 enough to fund failed CMS1 buys (~$36 + buffer)
        TRANSFER_USDT = 40.0
        if cms2_usdt < TRANSFER_USDT:
            print(f"!! CMS2 USDT ({cms2_usdt:.2f}) below transfer target ({TRANSFER_USDT}). Aborting.")
            return
        print(f"\n========== TRANSFER: {TRANSFER_USDT} USDT CMS2 → CMS1 ==========")
        st, data = await rest.subaccount_transfer(
            from_id=creds["CM2_SUBACCOUNT_ID"], to_id=creds["CM1_SUBACCOUNT_ID"],
            currency="USDT", amount=str(TRANSFER_USDT),
        )
        print(f"  status={st} resp={data}")
        if st not in (200, 201, 202):
            print("!! Transfer failed. Aborting.")
            return
        await asyncio.sleep(2)

        # Phase C2a: retry CMS1 USDT buys (BITGOLD/VALR10/PUMP)
        print("\n========== PHASE C2a: retry CMS1 USDT base buys ==========")
        for b in FAILED_BUYS_CMS1:
            ok, info = await execute_one(rest, creds, b, metas)
            print(f"     → {info}")
            await asyncio.sleep(0.3)

        # Phase C2b: retry ETH buys
        print("\n========== PHASE C2b: retry ETH buys ==========")
        for b in FAILED_BUYS_ETH:
            ok, info = await execute_one(rest, creds, b, metas)
            print(f"     → {info}")
            await asyncio.sleep(0.3)

        # Final balances
        await asyncio.sleep(2)
        print("\n========== FINAL BALANCES ==========")
        for tag, sub in (("CMS1", creds["CM1_SUBACCOUNT_ID"]), ("CMS2", creds["CM2_SUBACCOUNT_ID"])):
            bals = await rest.balances(sub)
            print(f"\n--- {tag} ---")
            for b in bals:
                avail = float(b.get("available", 0))
                if avail > 0.0001:
                    print(f"  {b['currency']}: {avail}")


if __name__ == "__main__":
    asyncio.run(main())
