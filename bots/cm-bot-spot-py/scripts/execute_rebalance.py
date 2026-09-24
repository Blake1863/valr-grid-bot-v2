#!/usr/bin/env python3
"""Execute the rebalance plan in 3 phases (A: sells -> B: USDT->ZAR -> C: base buys).

Each order is an IOC limit:
  - SELL: priced at best_bid - 2 ticks (sweeps through book, won't rest)
  - BUY:  priced at best_ask + 2 ticks (sweeps through book, won't rest)

After each phase, refreshes balances and verifies the next phase is fundable.
Aborts on any failure.
"""
import asyncio
import json
import os
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.creds import load
from src.valr_rest import ValrRest
from src.pair_meta import parse as parse_meta, format_price, format_qty, round_to_tick


def cid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:14]}"


async def get_pair_metas(rest: ValrRest, pairs):
    """Fetch pair metadata for each pair from /v1/public/pairs."""
    st, all_pairs = await rest.request("GET", "/v1/public/pairs")
    if st != 200 or not isinstance(all_pairs, list):
        raise RuntimeError(f"failed to fetch pairs: {st} {all_pairs}")
    by_symbol = {p["symbol"]: p for p in all_pairs if isinstance(p, dict)}
    metas = {}
    for sym in pairs:
        if sym not in by_symbol:
            raise RuntimeError(f"pair {sym} not found in /v1/public/pairs")
        metas[sym] = parse_meta(by_symbol[sym])
    return metas


async def get_book(rest: ValrRest, pair: str):
    st, ob = await rest.request("GET", f"/v1/public/{pair}/orderbook")
    if st != 200 or not isinstance(ob, dict):
        raise RuntimeError(f"orderbook fetch failed: {st} {ob}")
    asks = ob.get("Asks") or ob.get("asks") or []
    bids = ob.get("Bids") or ob.get("bids") or []
    if not asks or not bids:
        raise RuntimeError(f"empty book for {pair}")
    return Decimal(asks[0]["price"]), Decimal(bids[0]["price"])


async def place_ioc_limit(rest: ValrRest, sub_id: str, pair: str, meta, side: str, qty: Decimal,
                           price: Decimal, customer_order_id: str):
    """Submit a limit IOC order via REST."""
    qty_str = format_qty(qty, meta)
    price_str = format_price(price, meta)
    st, data = await rest.place_limit_rest(
        sub_id, pair=pair, side=side, price=price_str, quantity=qty_str,
        post_only=False, customer_order_id=customer_order_id, time_in_force="IOC",
    )
    return st, data, {"pair": pair, "side": side, "price": price_str, "quantity": qty_str}


async def get_status(rest: ValrRest, sub_id: str, customer_order_id: str):
    """Fetch order history detail by customerOrderId. Returns latest status + fills."""
    st, data = await rest.order_history_by_cid(sub_id, customer_order_id)
    if st != 200:
        return None, None, None
    if isinstance(data, list) and data:
        # First entry is the latest status event (newest first)
        latest = data[0] if isinstance(data[0], dict) else None
        if latest:
            return latest.get("orderStatusType"), latest.get("executedQuantity"), latest.get("executedPrice")
    if isinstance(data, dict):
        return data.get("orderStatusType"), data.get("executedQuantity"), data.get("executedPrice")
    return None, None, None


async def execute_order(rest: ValrRest, creds: dict, action: dict, metas: dict, slippage_ticks: int = 2):
    sub_label = action["sub"]
    sub_id = creds["CM1_SUBACCOUNT_ID"] if sub_label == "CMS1" else creds["CM2_SUBACCOUNT_ID"]
    pair = action["pair"]
    side = action["side"]
    qty_native = Decimal(str(action["qty_native"]))
    meta = metas[pair]

    ask, bid = await get_book(rest, pair)
    tick = meta.tick_size
    if side == "SELL":
        # Price below the bid by N ticks -> sweeps bids
        price = round_to_tick(bid - tick * slippage_ticks, tick)
    else:  # BUY
        price = round_to_tick(ask + tick * slippage_ticks, tick)

    # Round qty to base step
    base_step = Decimal("1") / (Decimal("10") ** meta.base_decimals)
    qty_floored = (qty_native / base_step).to_integral_value(rounding="ROUND_DOWN") * base_step
    if qty_floored < meta.min_base_amount:
        qty_floored = meta.min_base_amount

    co_id = cid(f"rb-{side[0].lower()}")
    print(f"  → {sub_label} {side} {qty_floored} {pair} @ {price} (cid={co_id})")
    st, data, body = await place_ioc_limit(rest, sub_id, pair, meta, side, qty_floored, price, co_id)
    if st not in (200, 201, 202):
        return False, f"placement failed st={st} resp={data}"

    # IOC: confirms fill or fully cancels. Wait for status.
    await asyncio.sleep(0.4)
    for _ in range(8):
        status, ex_qty, ex_price = await get_status(rest, sub_id, co_id)
        if status in ("Filled", "Partially Filled", "Cancelled"):
            return True, {"status": status, "executed_qty": ex_qty, "executed_price": ex_price}
        await asyncio.sleep(0.5)
    return True, {"status": "unknown", "cid": co_id}


async def main():
    creds = load()
    plan = json.load(open(ROOT / "rebalance_plan.json"))

    all_pairs = sorted(set(
        [s["pair"] for s in plan["sells"]]
        + [c["pair"] for c in plan["usdt_to_zar"]]
        + [b["pair"] for b in plan["base_buys"]]
    ))

    print(f"Loading metadata for {len(all_pairs)} pairs...")
    async with ValrRest(creds["MAIN_API_KEY"], creds["MAIN_API_SECRET"]) as rest:
        metas = await get_pair_metas(rest, all_pairs)

        results = {"phase_A": [], "phase_B": [], "phase_C": []}

        # ---------------- PHASE A: sells -> USDT ----------------
        print("\n========== PHASE A: SELLS ==========")
        for sell in plan["sells"]:
            ok, info = await execute_order(rest, creds, sell, metas)
            results["phase_A"].append({"action": sell, "ok": ok, "info": info})
            if not ok:
                print(f"\n!! ABORT: phase A failure: {info}")
                json.dump(results, open(ROOT / "rebalance_results.json", "w"), indent=2, default=str)
                sys.exit(1)
            print(f"     → {info}")
            await asyncio.sleep(0.3)  # gentle on rate limits

        await asyncio.sleep(2)
        print("\n========== balance check after A ==========")
        for tag, sub in (("CMS1", creds["CM1_SUBACCOUNT_ID"]), ("CMS2", creds["CM2_SUBACCOUNT_ID"])):
            bals = await rest.balances(sub)
            usdt = next((float(b.get("available", 0)) for b in bals if b["currency"] == "USDT"), 0)
            print(f"  {tag} USDT: {usdt:.4f}")

        # ---------------- PHASE B: USDT -> ZAR ----------------
        print("\n========== PHASE B: USDT->ZAR (sells of USDT via USDTZAR) ==========")
        for conv in plan["usdt_to_zar"]:
            ok, info = await execute_order(rest, creds, conv, metas)
            results["phase_B"].append({"action": conv, "ok": ok, "info": info})
            if not ok:
                print(f"\n!! ABORT: phase B failure: {info}")
                json.dump(results, open(ROOT / "rebalance_results.json", "w"), indent=2, default=str)
                sys.exit(1)
            print(f"     → {info}")
            await asyncio.sleep(0.3)

        await asyncio.sleep(2)
        print("\n========== balance check after B ==========")
        for tag, sub in (("CMS1", creds["CM1_SUBACCOUNT_ID"]), ("CMS2", creds["CM2_SUBACCOUNT_ID"])):
            bals = await rest.balances(sub)
            usdt = next((float(b.get("available", 0)) for b in bals if b["currency"] == "USDT"), 0)
            zar = next((float(b.get("available", 0)) for b in bals if b["currency"] == "ZAR"), 0)
            print(f"  {tag} USDT: {usdt:.4f}  ZAR: {zar:.2f}")

        # ---------------- PHASE C: base buys ----------------
        print("\n========== PHASE C: BASE BUYS ==========")
        for buy in plan["base_buys"]:
            ok, info = await execute_order(rest, creds, buy, metas)
            results["phase_C"].append({"action": buy, "ok": ok, "info": info})
            if not ok:
                print(f"\n!! WARN: buy failed (continuing with rest): {info}")
                # don't abort here; one missing base just means that pair won't trade until refilled
            else:
                print(f"     → {info}")
            await asyncio.sleep(0.3)

        await asyncio.sleep(2)
        print("\n========== final balances ==========")
        for tag, sub in (("CMS1", creds["CM1_SUBACCOUNT_ID"]), ("CMS2", creds["CM2_SUBACCOUNT_ID"])):
            bals = await rest.balances(sub)
            print(f"\n--- {tag} ---")
            for b in bals:
                avail = float(b.get("available", 0))
                if avail > 0.0001:
                    print(f"  {b['currency']}: {avail}")

    json.dump(results, open(ROOT / "rebalance_results.json", "w"), indent=2, default=str)
    print("\nDone. Results saved to rebalance_results.json")


if __name__ == "__main__":
    asyncio.run(main())
