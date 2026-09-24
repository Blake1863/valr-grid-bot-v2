#!/usr/bin/env python3
"""Transfer already-bought base from MAIN to both CMS subs (50/50 each).
Used when market buys already completed but transfers failed.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.valr_rest import ValrRest

SUB_ACCOUNTS = {
    "zar":  {"A": 1513524239074144256, "B": 1513524288865144832},
    "usdt": {"A": 1513524297399840768, "B": 1513524305939443712},
}

ZAR_PAIRS = ["BTCZAR", "ETHZAR", "SOLZAR", "AVAXZAR", "LINKZAR", "XAUTZAR", "XRPZAR", "USDCZAR", "BNBZAR"]
USDT_PAIRS = ["BITGOLDUSDT", "COINXUSDT", "CRCLXUSDT", "JUPUSDT", "NVDAXUSDT", "PUMPUSDT",
              "TRUMPUSDT", "TSLAXUSDT", "VALR10USDT", "MSTRXUSDT", "HOODXUSDT"]

async def main():
    env_path = os.path.join(os.path.dirname(__file__), "../../cm-bot-spot/.env")
    api_key = None
    api_secret = None
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("MAIN_API_KEY="):
                api_key = line.split("=", 1)[1]
            elif line.startswith("MAIN_API_SECRET="):
                api_secret = line.split("=", 1)[1]

    if not api_key or not api_secret:
        print("ERROR: Missing creds")
        sys.exit(1)

    async with ValrRest(api_key, api_secret) as rest:
        # Re-check main balances
        bals = await rest.balances("")
        bal_map = {}
        for b in bals:
            code = b.get("currency", b.get("currencyCode", "?"))
            avail = float(b.get("available", b.get("balance", "0")))
            bal_map[code] = avail

        print("=== Balances to transfer ===")
        for code, avail in sorted(bal_map.items()):
            if avail > 0:
                print(f"  {code}: {avail}")

        print("\n=== Transferring ===")
        total_transfers = 0
        success_count = 0

        for pair in ZAR_PAIRS:
            base = pair.replace("ZAR", "")
            avail = bal_map.get(base, 0)
            if avail <= 0:
                print(f"  SKIP {base}: no balance")
                continue
            transfer_amt = avail * 0.5
            transfer_amt = int(transfer_amt * 1e8) / 1e8

            for label, sub_id in [("A", SUB_ACCOUNTS["zar"]["A"]), ("B", SUB_ACCOUNTS["zar"]["B"])]:
                print(f"  Transferring {transfer_amt} {base} → CMSZAR{label} (sub {sub_id})...")
                total_transfers += 1
                try:
                    st, result = await rest.subaccount_transfer(
                        from_id=0, to_id=sub_id, currency=base, amount=str(transfer_amt)
                    )
                    if st in (200, 201, 202):
                        print(f"    ✅ {st}")
                        success_count += 1
                    else:
                        print(f"    ❌ {st} {result}")
                except Exception as e:
                    print(f"    ❌ Exception: {e}")
                await asyncio.sleep(0.5)

        for pair in USDT_PAIRS:
            base = pair.replace("USDT", "")
            avail = bal_map.get(base, 0)
            if avail <= 0:
                print(f"  SKIP {base}: no balance")
                continue
            transfer_amt = avail * 0.5
            transfer_amt = int(transfer_amt * 1e8) / 1e8

            for label, sub_id in [("A", SUB_ACCOUNTS["usdt"]["A"]), ("B", SUB_ACCOUNTS["usdt"]["B"])]:
                print(f"  Transferring {transfer_amt} {base} → CMSUSDT{label} (sub {sub_id})...")
                total_transfers += 1
                try:
                    st, result = await rest.subaccount_transfer(
                        from_id=0, to_id=sub_id, currency=base, amount=str(transfer_amt)
                    )
                    if st in (200, 201, 202):
                        print(f"    ✅ {st}")
                        success_count += 1
                    else:
                        print(f"    ❌ {st} {result}")
                except Exception as e:
                    print(f"    ❌ Exception: {e}")
                await asyncio.sleep(0.5)

        print(f"\nDone: {success_count}/{total_transfers} transfers succeeded")


if __name__ == "__main__":
    asyncio.run(main())
