#!/usr/bin/env python3
"""Reseed thin or empty USDT-quoted spot pairs on both wash subaccounts.

Default mode is dry-run. Pass --execute to place market BUY orders.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, "src")
from creds import load  # noqa: E402
from valr_rest import ValrRest  # noqa: E402


PAIRS = [
    "SPYXUSDT",
    "NVDAXUSDT",
    "COINXUSDT",
    "TRUMPUSDT",
    "TSLAXUSDT",
    "PUMPUSDT",
    "VALR10USDT",
    "JUPUSDT",
]


@dataclass
class AccountTarget:
    label: str
    sub_id: str


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target-usd", type=float, default=6.0, help="Target base inventory value per account per pair.")
    p.add_argument("--buffer-usd", type=float, default=0.3, help="Extra quote to spend above the computed deficit.")
    p.add_argument("--min-order-usd", type=float, default=1.0, help="Skip buys below this quote amount.")
    p.add_argument("--execute", action="store_true", help="Place live market BUY orders.")
    return p.parse_args()


async def get_balances(rest: ValrRest, sub_id: str) -> dict[str, float]:
    rows = await rest.balances(sub_id)
    return {row["currency"]: float(row.get("available", 0) or 0) for row in rows}


async def get_order_types(rest: ValrRest, pair: str) -> list[str]:
    st, data = await rest.request("GET", f"/v1/public/{pair}/ordertypes", signed=False)
    if st != 200 or not isinstance(data, list):
        raise RuntimeError(f"ordertypes {pair} failed: {st} {data}")
    return data


async def market_buy(rest: ValrRest, sub_id: str, pair: str, quote_amount: str) -> tuple[int, object]:
    body = {
        "pair": pair,
        "side": "BUY",
        "quoteAmount": quote_amount,
        "timeInForce": "IOC",
        "customerOrderId": f"reseed-{uuid.uuid4().hex[:16]}",
        "allowMargin": False,
    }
    return await rest.request("POST", "/v1/orders/market", body, subaccount_id=sub_id)


async def main() -> int:
    args = parse_args()
    creds = load()
    rest = ValrRest(creds["MAIN_API_KEY"], creds["MAIN_API_SECRET"])
    await rest.__aenter__()
    try:
        accounts = [
            AccountTarget("CMSUSDT1", "1513524297399840768"),
            AccountTarget("CMSUSDT2", "1513524305939443712"),
        ]
        balances = {acct.sub_id: await get_balances(rest, acct.sub_id) for acct in accounts}

        print(f"mode={'EXECUTE' if args.execute else 'DRY-RUN'} target_usd={args.target_usd:.2f} buffer_usd={args.buffer_usd:.2f}")
        print()

        for pair in PAIRS:
            meta = await rest.public_marketsummary(pair)
            order_types = await get_order_types(rest, pair)
            if "MARKET" not in order_types:
                print(f"{pair}: skip (MARKET not supported) order_types={order_types}")
                continue

            bid = float(meta["bidPrice"])
            ask = float(meta["askPrice"])
            last = float(meta["lastTradedPrice"])
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
            base = pair[:-4]

            print(f"{pair}: price~{mid:.6f}")
            for acct in accounts:
                raw = balances[acct.sub_id].get(base, 0.0)
                value = raw * mid
                deficit = max(0.0, args.target_usd - value)
                if deficit < args.min_order_usd:
                    print(f"  {acct.label}: OK raw={raw:.8f} value=${value:.2f}")
                    continue
                spend = deficit + args.buffer_usd
                spend_s = f"{spend:.2f}"
                print(f"  {acct.label}: raw={raw:.8f} value=${value:.2f} -> buy ${spend_s}")
                if not args.execute:
                    continue
                st, data = await market_buy(rest, acct.sub_id, pair, spend_s)
                print(f"    status={st} data={json.dumps(data, separators=(',', ':'))}")
                await asyncio.sleep(1.0)
                balances[acct.sub_id] = await get_balances(rest, acct.sub_id)
            print()

        print("final balances:")
        balances = {acct.sub_id: await get_balances(rest, acct.sub_id) for acct in accounts}
        for acct in accounts:
            usdt = balances[acct.sub_id].get("USDT", 0.0)
            print(f"  {acct.label}: USDT={usdt:.8f}")
        return 0
    finally:
        await rest.__aexit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
