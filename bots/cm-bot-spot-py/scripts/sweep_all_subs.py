#!/usr/bin/env python3
"""Sweep ALL CMS subaccounts to MAIN, then liquidate everything to USDT on MAIN."""
import asyncio, sys, json, os

sys.path.insert(0, '/home/admin/.openclaw/workspace/bots/cm-bot-spot-py')
from src.valr_rest import ValrRest
from src.creds import load

CM1 = "1483472097578319872"
CM2 = "1483472079069155328"
MAIN = "0"

CMS_SUBS = {
    "CMSZAR1": "1513524239074144256",
    "CMSZAR2": "1513524288865144832",
    "CMSUSDT1": "1513524297399840768",
    "CMSUSDT2": "1513524305939443712",
    "CMSUSDC1": "1513524314495823872",
    "CMSUSDC2": "1513524323040333824",
    "CMSBTC1": "1518603875676921856",
    "CMSBTC2": "1518603693873459200",
}

EXCLUDE_CURRENCIES = {"USDT", "USDC"}  # quote currencies — transfer separately

async def get_balances(rest, sub_id, label):
    """Get non-zero balances from a subaccount."""
    r = await rest.request('GET', '/v1/account/balances', subaccount_id=sub_id)
    if isinstance(r, tuple):
        r = r[1]
    non_zero = {}
    for b in r:
        amt = float(b.get('available', 0))
        if amt > 0:
            non_zero[b['currency']] = amt
    return label, sub_id, non_zero

async def main():
    creds = load()
    rest = ValrRest(creds['MAIN_API_KEY'], creds['MAIN_API_SECRET'])
    
    # Step 1: Collect all balances
    print("=== Collecting balances ===")
    all_bals = {}
    for label, sid in CMS_SUBS.items():
        label_out, sid_out, bals = await get_balances(rest, sid, label)
        all_bals[label] = (sid, bals)
        total_ref = sum(bals.values())  # all reference USDC
        print(f"  {label} ({sid}): {len(bals)} non-zero currencies, ~${total_ref:.2f}")
        for cur, amt in sorted(bals.items(), key=lambda x: x[1]):
            print(f"    {cur}: {amt}")
    
    # Step 2: Transfer base coins to MAIN
    print("\n=== Transfers to MAIN ===")
    transfers = []
    for label, (sid, bals) in all_bals.items():
        for cur, amt in bals.items():
            # Skip if excluded
            if cur in EXCLUDE_CURRENCIES:
                continue
            # Leave tiny dust (keep > 0.9999 of balance)
            transfer_amt = amt * 0.9999
            transfers.append((sid, cur, transfer_amt, label))
            print(f"  {label} -> MAIN: {cur} {transfer_amt:.10f}")
    
    print(f"\nTotal transfers planned: {len(transfers)}")
    
    if '--execute' not in sys.argv:
        print("DRY RUN. Pass --execute to run.")
        return
    
    # Execute transfers
    for sid, cur, amt, label in transfers:
        if amt < 0.00000001:
            continue
        try:
            r = await rest.request('POST', '/v1/account/subaccounts/transfer',
                body={
                    "fromId": int(sid),
                    "toId": int(MAIN),
                    "currencyCode": cur,
                    "amount": f"{amt:.10f}",
                    "allowBorrow": False
                })
            print(f"  OK: {label} {cur} {amt:.10f} -> MAIN: {r}")
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"  FAIL: {label} {cur} {amt:.10f} -> MAIN: {e}")
    
    print("\n=== Waiting for balances to update ===")
    await asyncio.sleep(10)
    
    # Step 3: Check MAIN balances
    r = await rest.request('GET', '/v1/account/balances')
    if isinstance(r, tuple):
        r = r[1]
    main_bals = {}
    for b in r:
        amt = float(b.get('available', 0))
        if amt > 0 and b['currency'] not in {'ZAR', 'USD'}:
            main_bals[b['currency']] = amt
    
    print("=== MAIN balances after transfers ===")
    for cur, amt in sorted(main_bals.items(), key=lambda x: x[1]):
        print(f"  {cur}: {amt}")
    
    # Step 4: Liquidate base coins to USDT
    print("\n=== Liquidation plan ===")
    sells = []
    for cur, amt in main_bals.items():
        if cur in {'USDT', 'USDC'}:
            continue
        if amt < 0.00000001:
            continue
        # Find the pair
        pair = f"{cur}USDT"
        sells.append((pair, amt))
        print(f"  Market SELL {amt:.10f} {pair}")
    
    if '--execute' not in sys.argv:
        print("DRY RUN. Pass --execute to run.")
        return
    
    for pair, amt in sells:
        try:
            r = await rest.request('POST', '/v1/orders/market',
                body={"side": "SELL", "baseAmount": f"{amt:.10f}", "pair": pair})
            print(f"  OK: SELL {pair} {amt:.10f}: {r}")
            await asyncio.sleep(1)
        except Exception as e:
            print(f"  FAIL: SELL {pair} {amt:.10f}: {e}")
    
    # Step 5: Sweep remaining USDC to USDT if meaningful
    print("\n=== Done ===")

asyncio.run(main())
