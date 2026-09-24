#!/usr/bin/env python3
"""Restock wash-bot inventory at 3x the minimum ($72/pair instead of $24).
Buys base on MAIN account, then transfers 50/50 to both CMS subs.

Usage: python3 restock_3x.py [--dry-run]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.valr_rest import ValrRest

MAIN_ID = ""  # empty string = primary account for VALR API
SUB_ACCOUNTS = {
    "zar":  {"A": 1513524239074144256, "B": 1513524288865144832},
    "usdt": {"A": 1513524297399840768, "B": 1513524305939443712},
    "usdc": {"A": 1513524314495823872, "B": 1513524323040333824},
}

# 3x the standard $24/pair = $72
SPEND_USD = 72
ZAR_REF = 16.37  # ZAR/USD reference rate
SPEND_ZAR = int(SPEND_USD * ZAR_REF)  # ~R1,177 per ZAR pair

# Pairs and their base currencies
ZAR_PAIRS = ["BTCZAR", "ETHZAR", "SOLZAR", "AVAXZAR", "LINKZAR", "XAUTZAR", "XRPZAR", "USDCZAR"]
USDT_PAIRS = ["BITGOLDUSDT", "COINXUSDT", "CRCLXUSDT", "JUPUSDT", "NVDAXUSDT", "PUMPUSDT",
              "TRUMPUSDT", "TSLAXUSDT", "VALR10USDT"]
# BNBZAR and MSTRXUSDT and HOODXUSDT skipped — restock script flagged 17 pairs, 
# but the last check showed BNBZAR/MSTRXUSDT/HOODXUSDT were also thin. Let's include them.
ZAR_PAIRS_EXTRA = ["BNBZAR"]
USDT_PAIRS_EXTRA = ["MSTRXUSDT", "HOODXUSDT"]

ALL_ZAR = ZAR_PAIRS + ZAR_PAIRS_EXTRA
ALL_USDT = USDT_PAIRS + USDT_PAIRS_EXTRA

DRY_RUN = "--dry-run" in sys.argv


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
        print("ERROR: Missing MAIN_API_KEY or MAIN_API_SECRET")
        sys.exit(1)

    async with ValrRest(api_key, api_secret) as rest:
        # 1. Check main balances
        print("=== Main Account Balances ===")
        try:
            bals = await rest.balances(MAIN_ID)
        except Exception as e:
            print(f"ERROR: {e}")
            sys.exit(1)
        bal_map = {}
        if isinstance(bals, list):
            for b in bals:
                code = b.get("currency", b.get("currencyCode", "?"))
                avail = b.get("available", b.get("balance", "0"))
                total = b.get("total", "0")
                bal_map[code] = {"available": avail, "total": total}
                print(f"  {code}: avail={avail} total={total}")
        else:
            print(f"  ERROR: {st} {bals}")
            sys.exit(1)

        # 2. Check if we need ZAR — convert USDT→ZAR if short
        zar_needed = SPEND_ZAR * len(ALL_ZAR)
        zar_available = float(bal_map.get("ZAR", {}).get("available", "0"))
        print(f"\n=== ZAR Budget ===")
        print(f"  Needed for {len(ALL_ZAR)} ZAR pairs @ R{SPEND_ZAR}: R{zar_needed}")
        print(f"  Available: R{zar_available}")

        if zar_available < zar_needed and not DRY_RUN:
            usdt_to_sell = (zar_needed - zar_available) / ZAR_REF + 10  # small buffer
            print(f"  ⚠️ Short by R{zar_needed - zar_available:.0f}, converting ${usdt_to_sell:.2f} USDT → ZAR")
            
            # Check USDT balance first
            usdt_avail = float(bal_map.get("USDT", {}).get("available", "0"))
            if usdt_avail < usdt_to_sell:
                print(f"  ERROR: Insufficient USDT (have {usdt_avail}, need {usdt_to_sell})")
                sys.exit(1)

            st, result = await rest.request("POST", "/v1/orders/market", {
                "side": "SELL",
                "baseAmount": str(round(usdt_to_sell, 2)),
                "pair": "USDTZAR",
            })
            print(f"  USDT→ZAR conversion: {st} {result}")
            if st not in (200, 201, 202):
                print(f"  FAILED conversion, aborting")
                sys.exit(1)
            # Wait for settlement
            print("  Waiting 5s for settlement...")
            await asyncio.sleep(5)

            # Re-check balances
            bals = await rest.balances(MAIN_ID)
            if isinstance(bals, list):
                for b in bals:
                    code = b.get("currency", b.get("currencyCode", "?"))
                    avail = b.get("available", b.get("balance", "0"))
                    bal_map[code] = {"available": avail, "total": total}

        elif DRY_RUN:
            if zar_available < zar_needed:
                print(f"  [DRY-RUN] Would convert ~${(zar_needed - zar_available) / ZAR_REF:.0f} USDT → ZAR")
            else:
                print(f"  ✅ ZAR balance sufficient")

        # 3. Buy base currencies on main
        print(f"\n=== Buying Base on Main (quoteAmount) ===")

        # ZAR pairs: buy using ZAR quote
        for pair in ALL_ZAR:
            base = pair.replace("ZAR", "")
            print(f"  Buying {base} on {pair} @ ~R{SPEND_ZAR}...")
            if DRY_RUN:
                print(f"    [DRY-RUN] POST /v1/orders/market {{side:BUY, quoteAmount:{SPEND_ZAR}, pair:{pair}}}")
                continue
            st, result = await rest.request("POST", "/v1/orders/market", {
                "side": "BUY",
                "quoteAmount": str(SPEND_ZAR),
                "pair": pair,
            })
            print(f"    {st} {json.dumps(result, indent=2) if isinstance(result, dict) else result}")
            if st not in (200, 201, 202):
                print(f"    ⚠️ Failed — continuing anyway")
            await asyncio.sleep(1)  # rate limit buffer

        # USDT pairs: buy using USDT quote
        for pair in ALL_USDT:
            base = pair.replace("USDT", "")
            print(f"  Buying {base} on {pair} @ ~${SPEND_USD}...")
            if DRY_RUN:
                print(f"    [DRY-RUN] POST /v1/orders/market {{side:BUY, quoteAmount:{SPEND_USD}, pair:{pair}}}")
                continue
            st, result = await rest.request("POST", "/v1/orders/market", {
                "side": "BUY",
                "quoteAmount": str(SPEND_USD),
                "pair": pair,
            })
            print(f"    {st} {json.dumps(result, indent=2) if isinstance(result, dict) else result}")
            if st not in (200, 201, 202):
                print(f"    ⚠️ Failed — continuing anyway")
            await asyncio.sleep(1)

        if DRY_RUN:
            print(f"\n[DRY-RUN] Done. No actual orders placed.")
            return

        # 4. Wait for order settlement
        print("\nWaiting 10s for orders to settle...")
        await asyncio.sleep(10)

        # 5. Check main balances after buys
        try:
            bals = await rest.balances(MAIN_ID)
        except Exception as e:
            print(f"ERROR re-checking balances: {e}")
            sys.exit(1)
        post_bal_map = {}
        if isinstance(bals, list):
            for b in bals:
                code = b.get("currency", b.get("currencyCode", "?"))
                avail = b.get("available", b.get("balance", "0"))
                post_bal_map[code] = avail

        print(f"\n=== Post-Buy Balances ===")
        for code in sorted(post_bal_map.keys()):
            if float(post_bal_map[code]) > 0:
                print(f"  {code}: {post_bal_map[code]}")

        # 6. Transfer to subs
        print(f"\n=== Transferring to Subs (50/50) ===")
        
        # ZAR pairs
        for pair in ALL_ZAR:
            base = pair.replace("ZAR", "")
            avail = float(post_bal_map.get(base, "0"))
            if avail <= 0:
                print(f"  SKIP {base}: no balance")
                continue
            # Leave a tiny amount in main to avoid rounding issues
            transfer_amt = avail * 0.5  # 50% to each sub
            # Round down to avoid insufficient balance
            transfer_amt = int(transfer_amt * 1e8) / 1e8

            for label, sub_id in [("A", SUB_ACCOUNTS["zar"]["A"]), ("B", SUB_ACCOUNTS["zar"]["B"])]:
                print(f"  Transferring {transfer_amt} {base} → CMSZAR{label}...")
                st, result = await rest.subaccount_transfer(
                    from_id=0, to_id=sub_id, currency=base, amount=str(transfer_amt)
                )
                if st in (200, 201, 202):
                    print(f"    ✅ {st}")
                else:
                    print(f"    ❌ {st} {result}")
                await asyncio.sleep(0.5)

        # USDT pairs
        for pair in ALL_USDT:
            base = pair.replace("USDT", "")
            avail = float(post_bal_map.get(base, "0"))
            if avail <= 0:
                print(f"  SKIP {base}: no balance")
                continue
            transfer_amt = avail * 0.5
            transfer_amt = int(transfer_amt * 1e8) / 1e8

            for label, sub_id in [("A", SUB_ACCOUNTS["usdt"]["A"]), ("B", SUB_ACCOUNTS["usdt"]["B"])]:
                print(f"  Transferring {transfer_amt} {base} → CMSUSDT{label}...")
                st, result = await rest.subaccount_transfer(
                    from_id=0, to_id=sub_id, currency=base, amount=str(transfer_amt)
                )
                if st in (200, 201, 202):
                    print(f"    ✅ {st}")
                else:
                    print(f"    ❌ {st} {result}")
                await asyncio.sleep(0.5)

        print(f"\n✅ Restock complete!")
        print(f"  {len(ALL_ZAR)} ZAR pairs + {len(ALL_USDT)} USDT pairs")
        print(f"  Total spent: ~${SPEND_USD * (len(ALL_ZAR) + len(ALL_USDT))} (plus ZAR conversion)")
        print(f"  Monitor logs for prints resuming in ~90s")


if __name__ == "__main__":
    asyncio.run(main())
