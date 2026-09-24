#!/usr/bin/env python3
"""Calculate full restock plan to revive all wash pairs.
Sells excess base from subs → MAIN, then buys & distributes to target ~$35/pair.
Run dry-run first, pass --execute to act.
"""
import sys, asyncio, json
sys.path.insert(0, '.'); from src.creds import load
from src.valr_rest import ValrRest

SUB_IDS = {
    'ZAR': [1513524239074144256, 1513524288865144832],
    'USDT': [1513524297399840768, 1513524305939443712],
    'USDC': [1513524314495823872, 1513524323040333824],
}
SUB_NAMES = {
    1513524239074144256: 'CMSZAR1', 1513524288865144832: 'CMSZAR2',
    1513524297399840768: 'CMSUSDT1', 1513524305939443712: 'CMSUSDT2',
    1513524314495823872: 'CMSUSDC1', 1513524323040333824: 'CMSUSDC2',
}
TARGET_USD = 35.0  # per pair total (~$17.50 per sub)

# ZAR/USD reference for sizing
ZAR_USD_REF = 16.50

async def main(execute=False):
    c = load()
    main_key = c.get('MAIN_API_' + 'KEY')
    main_secret = c.get('MAIN_API_' + 'SECRET')
    rest = ValrRest(main_key, main_secret)

    # 1. Get all sub balances
    all_subs = {}
    for bucket, ids in SUB_IDS.items():
        for sid in ids:
            status, data = await rest.request('GET', '/v1/account/balances', subaccount_id=sid)
            all_subs[sid] = {'bucket': bucket, 'name': SUB_NAMES[sid], 'balances': {}}
            for b in data:
                avail = float(b.get('available', 0))
                if avail > 0:
                    all_subs[sid]['balances'][b['currencyCode']] = avail

    # 2. Get MAIN balances
    status, main_data = await rest.request('GET', '/v1/account/balances')
    main_bals = {}
    for b in main_data:
        avail = float(b.get('available', 0))
        if avail > 0:
            main_bals[b['currencyCode']] = avail

    # 3. Get market prices
    all_pairs = set()
    for sid, info in all_subs.items():
        for cur in info['balances']:
            # Determine pairs for this currency
            bucket = info['bucket']
            if bucket == 'ZAR':
                all_pairs.add(cur + 'ZAR')
            elif bucket == 'USDT':
                all_pairs.add(cur + 'USDT')
            elif bucket == 'USDC':
                all_pairs.add(cur + 'USDC')

    prices = {}
    await asyncio.sleep(1)
    async with __import__('aiohttp').ClientSession() as session:
        for p in sorted(all_pairs):
            try:
                async with session.get(f'https://api.valr.com/v1/public/{p}/marketsummary') as r:
                    text = await r.text()
                    if text.startswith('{'):
                        d = json.loads(text)
                        prices[p] = float(d['bidPrice'])  # use bid (what we'd sell at)
                    else:
                        prices[p] = None
            except:
                prices[p] = None
            await asyncio.sleep(0.5)

    # 4. Calculate current USD value of base per pair across subs
    pair_totals = {}  # pair -> {total_base, total_usd, subs: {sid: amount}}
    for sid, info in all_subs.items():
        bucket = info['bucket']
        for cur, amt in info['balances'].items():
            if cur in ('ZAR', 'USDT', 'USDC', 'EURC'):
                continue  # skip quote currency
            pair = cur + ('ZAR' if bucket == 'ZAR' else 'USDT' if bucket == 'USDT' else 'USDC')
            px = prices.get(pair)
            usd_val = None
            if px is not None:
                if bucket == 'ZAR':
                    usd_val = amt * px / ZAR_USD_REF
                else:
                    usd_val = amt * px
            if pair not in pair_totals:
                pair_totals[pair] = {'total_base': 0, 'total_usd': usd_val, 'subs': {}, 'bucket': bucket}
            pair_totals[pair]['total_base'] += amt
            pair_totals[pair]['subs'][sid] = amt
            if usd_val is not None:
                if pair_totals[pair]['total_usd'] is None:
                    pair_totals[pair]['total_usd'] = usd_val
                else:
                    pair_totals[pair]['total_usd'] += usd_val

    print("=" * 80)
    print("CURRENT INVENTORY VALUATION (base only, per pair)")
    print("=" * 80)
    total_usd = 0
    for pair in sorted(pair_totals.keys()):
        info = pair_totals[pair]
        usd = info['total_usd'] or 0
        total_usd += usd
        sub_vals = []
        for sid, amt in info['subs'].items():
            px = prices.get(pair)
            if px and info['bucket'] == 'ZAR':
                v = amt * px / ZAR_USD_REF
            elif px:
                v = amt * px
            else:
                v = 0
            sub_vals.append(f"{info['name'].replace(str(sid), SUB_NAMES.get(sid,'?'))}({SUB_NAMES[sid]}): ${v:.2f}")
        print(f"  {pair:15s}: base={info['total_base']:>14.8f}  ≈ ${usd:>8.2f}  ({', '.join(sub_vals)})")

    print(f"\n  TOTAL base inventory value: ${total_usd:.2f}")
    print(f"  MAIN quote balances: {main_bals}")

    # 5. Determine what to do
    print("\n" + "=" * 80)
    print("RESTOCK PLAN")
    print("=" * 80)

    sells = []  # (pair, sid, amount, quote_currency)
    buys = []   # (pair, quote_amount, quote_currency)
    transfers = []  # (currency, from_sub, to_sub, amount) or (currency, MAIN, sub, amount)

    for pair, info in sorted(pair_totals.items()):
        usd = info['total_usd'] or 0
        bucket = info['bucket']
        if bucket == 'ZAR':
            quote_cur = 'ZAR'
        elif bucket == 'USDT':
            quote_cur = 'USDT'
        else:
            quote_cur = 'USDC'

        if usd > TARGET_USD + 5:
            # Excess — sell down to target on both subs evenly
            excess = usd - TARGET_USD
            # Determine sell amount per sub
            px = prices.get(pair)
            if px is None:
                print(f"  ⚠️ {pair}: no price, skipping")
                continue
            for sid, amt in info['subs'].items():
                sub_usd = amt * px / ZAR_USD_REF if bucket == 'ZAR' else amt * px
                target_sub = TARGET_USD / 2  # $17.50 per sub
                if sub_usd > target_sub + 2.5:
                    excess_amt = (sub_usd - target_sub - 2.5)
                    sell_base = excess_amt * ZAR_USD_REF / px if bucket == 'ZAR' else excess_amt / px
                    sells.append((pair, sid, sell_base, quote_cur, SUB_NAMES[sid]))
                    print(f"  SELL {sell_base:>12.8f} {pair.split(quote_cur)[0]} on {SUB_NAMES[sid]} (excess ${excess_amt:.2f} over ${target_sub+2.5:.2f})")

        elif usd < TARGET_USD - 5:
            # Deficit — need to buy
            shortfall = TARGET_USD - usd
            print(f"  BUY  ${shortfall:.2f} of {pair} on MAIN")
            buys.append((pair, shortfall, quote_cur))

    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    total_sell_usd = 0
    for pair, sid, amt, qcur, sname in sells:
        px = prices.get(pair, 0) or 0
        sell_quote = amt * px / ZAR_USD_REF if qcur == 'ZAR' else amt * px
        total_sell_usd += sell_quote
    print(f"  Total to sell: ≈ ${total_sell_usd:.2f} (→ MAIN quote)")
    print(f"  Total to buy:  ≈ ${sum(b[1] for b in buys):.2f}")

    if not execute:
        print("\n  *** DRY RUN — review above, then run with --execute ***")
        return

    # 6. Execute sells
    print("\nExecuting sells...")
    for pair, sid, amt, qcur, sname in sells:
        base_cur = pair.replace(qcur, '')
        print(f"  Market SELL {amt} {base_cur}/{qcur} on {sname}...")
        # Need to sell on the sub, but the order goes to MAIN? No, sell on sub.
        # Actually we sell on the sub — the quote goes to the sub.
        # Then we transfer quote from sub to MAIN.
        status, data = await rest.request('POST', '/v1/orders/market',
            body={'side': 'SELL', 'baseAmount': f'{amt}', 'pair': pair},
            subaccount_id=sid)
        print(f"    → {status}: {data}")
        await asyncio.sleep(1)

    print("\nWaiting for sells to settle...")
    await asyncio.sleep(30)

    # 7. Transfer quote from subs to MAIN
    print("Transferring quote to MAIN...")
    for bucket, ids in SUB_IDS.items():
        qcur = bucket  # ZAR, USDT, USDC
        for sid in ids:
            status, data = await rest.request('GET', '/v1/account/balances', subaccount_id=sid)
            for b in data:
                if b['currencyCode'] == qcur:
                    avail = float(b.get('available', 0))
                    if avail > 1:
                        # Transfer to MAIN (from sub to main uses fromId=sub, toId=0)
                        print(f"  Transfer {avail} {qcur} from {SUB_NAMES[sid]} → MAIN")
                        status2, data2 = await rest.request('POST', '/v1/account/subaccounts/transfer',
                            body={'fromId': sid, 'toId': 0, 'currencyCode': qcur, 'amount': f'{avail}', 'allowBorrow': False})
                        print(f"    → {status2}")
                        await asyncio.sleep(2)

    await asyncio.sleep(30)

    # 8. Convert USDT→ZAR if needed (for ZAR bucket buys)
    status, main_bals = await rest.request('GET', '/v1/account/balances')
    main_dict = {}
    for b in main_bals:
        main_dict[b['currencyCode']] = float(b.get('available', 0))

    zar_needed = sum(b[1] for b in buys if b[2] == 'ZAR')
    zar_have = main_dict.get('ZAR', 0)
    if zar_needed > zar_have and main_dict.get('USDT', 0) > 0:
        convert_usdt = min(main_dict['USDT'], (zar_needed - zar_have) * ZAR_USD_REF)
        print(f"\nConverting {convert_usdt:.2f} USDT → ZAR...")
        status, data = await rest.request('POST', '/v1/orders/market',
            body={'side': 'SELL', 'baseAmount': f'{convert_usdt}', 'pair': 'USDTZAR'})
        print(f"  → {status}: {data}")
        await asyncio.sleep(30)

    # 9. Buy base on MAIN and distribute to subs
    for pair, usd_amt, qcur in buys:
        base_cur = pair.replace(qcur, '')
        if qcur == 'ZAR':
            quote_amount = usd_amt * ZAR_USD_REF
        else:
            quote_amount = usd_amt
        print(f"\nMarket BUY ${quote_amount:.2f} {qcur} of {base_cur} on MAIN...")
        status, data = await rest.request('POST', '/v1/orders/market',
            body={'side': 'BUY', 'quoteAmount': f'{quote_amount:.2f}', 'pair': pair})
        print(f"  → {status}")
        await asyncio.sleep(10)

        # Check how much base we got
        status, main_bals = await rest.request('GET', '/v1/account/balances')
        main_dict = {b['currencyCode']: float(b.get('available', 0)) for b in main_bals}
        base_total = main_dict.get(base_cur, 0)
        half = base_total / 2 * 0.999  # leave tiny bit in main

        bucket = 'ZAR' if qcur == 'ZAR' else ('USDT' if qcur == 'USDT' else 'USDC')
        for sid in SUB_IDS[bucket]:
            print(f"  Transfer {half:.8f} {base_cur} MAIN → {SUB_NAMES[sid]}")
            status2, _ = await rest.request('POST', '/v1/account/subaccounts/transfer',
                body={'fromId': 0, 'toId': sid, 'currencyCode': base_cur, 'amount': f'{half}', 'allowBorrow': False})
            print(f"    → {status2}")
            await asyncio.sleep(2)

    print("\n✅ Restock complete!")

if __name__ == '__main__':
    execute = '--execute' in sys.argv
    asyncio.run(main(execute=execute))
