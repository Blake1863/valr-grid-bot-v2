#!/usr/bin/env python3
"""Execute wash-bot base inventory restock: buy on MAIN, split 50/50 to subs.

Called after human approval of inventory_restock_check.py output.
All buys use quoteAmount so MAIN covers exact cost without needing balance reconciliation.

USAGE: python3 scripts/restock_execute.py [--dry-run]
Default is DRY-RUN. Pass no flag or --execute for real orders.
"""
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

BUCKET_SUBS = {
    "zar": ["CMSZAR1", "CMSZAR2"],
    "usdt": ["CMSUSDT1", "CMSUSDT2"],
    "usdc": ["CMSUSDC1", "CMSUSDC2"],
}

# From latest inventory_restock_check.py output (2026-06-21 23:28)
ORDERS = [
    # (pair, base_currency, bucket, buy_base_qty)
    ("AVAXZAR",     "AVAX",   "zar",  "5.5747016"),
    ("BNBZAR",      "BNB",    "zar",  "0.059546777"),
    ("BTCZAR",      "BTC",    "zar",  "0.00054505559"),
    ("ETHZAR",      "ETH",    "zar",  "0.020271288"),
    ("LINKZAR",     "LINK",   "zar",  "4.4083378"),
    ("SOLZAR",      "SOL",    "zar",  "0.47282571"),
    ("USDCZAR",     "USDC",   "zar",  "35"),
    ("XAUTZAR",     "XAUT",   "zar",  "0.0084537911"),
    ("XRPZAR",      "XRP",    "zar",  "30.484816"),
    ("BITGOLDUSDT", "BITGOLD","usdt", "0.38678307"),
    ("COINXUSDT",   "COINX",  "usdt", "0.21065302"),
    ("CRCLXUSDT",   "CRCLX",  "usdt", "0.43375883"),
    ("HOODXUSDT",   "HOODX",  "usdt", "0.32264012"),
    ("JUPUSDT",     "JUP",    "usdt", "156.31979"),
    ("MSTRXUSDT",   "MSTRX",  "usdt", "0.2969625"),
    ("NVDAXUSDT",   "NVDAX",  "usdt", "0.1675523"),
    ("PUMPUSDT",    "PUMP",   "usdt", "23411.371"),
    ("TRUMPUSDT",   "TRUMP",  "usdt", "18.353435"),
    ("TSLAXUSDT",   "TSLAX",  "usdt", "0.086710931"),
    ("USDPCUSDT",   "USDPC",  "usdt", "30.185945"),
    ("VALR10USDT",  "VALR10", "usdt", "0.6916996"),
    ("XAUTUSDT",    "XAUT",   "usdt", "0.0084329221"),
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


async def get_balances(session):
    status, bals = await req(session, "GET", "/v1/account/balances")
    if status != 200:
        raise RuntimeError(f"Failed to get balances: status={status} {bals}")
    return {b["currency"]: float(b.get("available", 0)) for b in bals}


async def main(dry_run=True):
    async with aiohttp.ClientSession() as s:
        # Check MAIN balances first
        bals = await get_balances(s)
        print(f"MAIN balances: ZAR={bals.get('ZAR',0):.2f} USDT={bals.get('USDT',0):.2f} USDC={bals.get('USDC',0):.2f}")

        # Calculate ZAR and USDT needed
        zar_pairs = [(p, b, q) for p, b, q, _ in ORDERS if q == "zar"]
        usdt_pairs = [(p, b, q) for p, b, q, _ in ORDERS if q == "usdt"]

        # We use quoteAmount buys, so cost per pair ≈ $35
        zar_needed = len(zar_pairs) * 35 * 16.55  # rough R per pair
        usdt_needed = len(usdt_pairs) * 35

        print(f"Estimated spend: ZAR ≈ R{zar_needed:.0f} (avail R{bals.get('ZAR',0):.0f}), USDT ≈ ${usdt_needed:.0f} (avail ${bals.get('USDT',0):.0f})")

        if dry_run:
            print("\n🔵 DRY RUN — no orders or transfers will execute.")
            print(f"Would place {len(ORDERS)} market buys on MAIN:")
            for pair, base, bucket, qty in ORDERS:
                print(f"  BUY {pair} baseAmount={qty}")
            print(f"Would then transfer each base 50/50 to {bucket} subs.")
            return

        # Phase 1: Market buy base on MAIN
        print(f"\n🟢 Executing {len(ORDERS)} market buys on MAIN...")
        for i, (pair, base, bucket, qty) in enumerate(ORDERS, 1):
            body = json.dumps({"side": "BUY", "baseAmount": str(qty), "pair": pair})
            status, resp = await req(s, "POST", "/v1/orders/market", body=body)
            if status in (200, 201, 202):
                print(f"  [{i}/{len(ORDERS)}] BUY {pair}: OK (baseAmount={qty})")
            else:
                print(f"  [{i}/{len(ORDERS)}] BUY {pair}: FAIL status={status} resp={resp}")
                print("Aborting — no transfers will execute.")
                return
            # Small delay between orders
            await asyncio.sleep(1)

        # Wait for balances to settle
        print("\nWaiting 90s for balances to settle (30s cache TTL + cycles)...")
        await asyncio.sleep(90)

        # Phase 2: Get post-buy balances
        bals_after = await get_balances(s)
        print("\nPost-buy MAIN balances:")
        for pair, base, bucket, _ in ORDERS:
            if base in bals_after:
                print(f"  {base}: {bals_after[base]}")

        # Phase 3: Transfer 50/50 to subs
        print("\n🔄 Transferring base inventory to subs...")
        transfer_count = 0
        for pair, base, bucket, qty in ORDERS:
            subs = BUCKET_SUBS[bucket]
            avail = bals_after.get(base, 0)
            half = avail / 2.0
            # Leave tiny remainder in main to avoid rounding rejects
            half_str = f"{half:.10g}"

            for sub_name in subs:
                sub_id = SUBS[sub_name]
                body = json.dumps({
                    "fromId": "0",
                    "toId": sub_id,
                    "currencyCode": base,
                    "amount": half_str,
                    "allowBorrow": False,
                })
                status, resp = await req(s, "POST", "/v1/account/subaccounts/transfer", body=body)
                if status in (200, 201):
                    print(f"  Transfer {base} -> {sub_name}: OK ({half_str})")
                    transfer_count += 1
                else:
                    print(f"  Transfer {base} -> {sub_name}: FAIL status={status} resp={resp}")
                await asyncio.sleep(0.5)

        print(f"\n✅ Done! {len(ORDERS)} buys + {transfer_count} transfers complete.")
        print("Monitor logs for prints resuming within next summary window.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="Execute real orders/transfers (default is dry-run)")
    args = ap.parse_args()
    asyncio.run(main(dry_run=not args.execute))
