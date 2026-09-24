#!/usr/bin/env python3
"""Revive all wash pairs.

Problem: ALL subs have ZERO quote currency (ZAR/USDT/USDC = 0).
Solution: Sell excess base on subs → buy thin pairs → distribute.

ZAR/USD ref: ~16.50 | Target: ~$35/pair (~$17.50/sub)
Dry-run by default. Pass --execute to act.
"""
import sys, asyncio, json, aiohttp
sys.path.insert(0, '.')
from src.creds import load
from src.valr_rest import ValrRest

SUB_IDS = {
    'ZAR': ['1513524239074144256', '1513524288865144832'],
    'USDT': ['1513524297399840768', '1513524305939443712'],
    'USDC': ['1513524314495823872', '1513524323040333824'],
}
SUB_NAMES = {
    '1513524239074144256': 'CMSZAR1', '1513524288865144832': 'CMSZAR2',
    '1513524297399840768': 'CMSUSDT1', '1513524305939443712': 'CMSUSDT2',
    '1513524314495823872': 'CMSUSDC1', '1513524323040333824': 'CMSUSDC2',
}
TARGET_USD = 35.0
ZAR_USD_REF = 16.50

async def get_bals(rest, sub_id=None):
    status, data = await rest.request('GET', '/v1/account/balances', subaccount_id=sub_id)
    r = {}
    for b in data:
        if not isinstance(b, dict):
            continue
        avail = float(b.get('available', 0))
        if avail > 0:
            key = b.get('currencyCode') or b.get('currency') or '?'
            r[key] = avail
    return r

async def get_px(session, pair):
    try:
        async with session.get(f'https://api.valr.com/v1/public/{pair}/marketsummary') as r:
            t = await r.text()
            if t.startswith('{'):
                return float(json.loads(t)['bidPrice'])
    except:
        pass
    return None

async def main(execute=False):
    c = load()
    main_key = c.get('MAIN_API_' + 'KEY')
    main_secret = c.get('MAIN_API_' + 'SECRET')
    rest = ValrRest(main_key, main_secret)

    # 1. All sub balances
    sub_bals = {}
    for bucket, ids in SUB_IDS.items():
        for sid in ids:
            sub_bals[sid] = await get_bals(rest, sub_id=sid)

    # 2. MAIN balances
    main_bals = await get_bals(rest)

    # 3. Build pair inventory
    pair_inv = {}
    all_px_pairs = set()
    for sid, bals in sub_bals.items():
        bucket = None
        for b, ids in SUB_IDS.items():
            if sid in ids:
                bucket = b; break
        for cur, amt in bals.items():
            if cur in ('ZAR', 'USDT', 'USDC', 'EURC'): continue
            pair = cur + bucket
            all_px_pairs.add(pair)
            if pair not in pair_inv:
                pair_inv[pair] = {'bucket': bucket, 'base_cur': cur, 'subs': {}, 'total_usd': 0}
            pair_inv[pair]['subs'][sid] = amt

    # 4. Get prices (with rate limit handling)
    global prices
    prices = {}
    async with aiohttp.ClientSession() as session:
        for p in sorted(all_px_pairs):
            prices[p] = await get_px(session, p)
            await asyncio.sleep(1.2)

    # 5. Show inventory
    print("=" * 90)
    print("SUB BASE INVENTORY vs TARGET ($35/pair = $17.50/sub)")
    print("=" * 90)
    for pair in sorted(pair_inv.keys()):
        info = pair_inv[pair]
        px = prices.get(pair)
        print(f"\n  {pair:15s} (bucket={info['bucket']}, px={px})")
        total_usd = 0
        for sid, amt in sorted(info['subs'].items()):
            bk = info['bucket']
            if bk == 'ZAR': usd = amt * px / ZAR_USD_REF if px else 0
            else: usd = amt * px if px else 0
            total_usd += usd
            diff = usd - TARGET_USD/2
            flag = '⚠️ OVER' if diff > 5 else ('⚠️ UNDER' if diff < -5 else '✓')
            print(f"    {SUB_NAMES[sid]:12s}: {amt:>14.8f} {info['base_cur']} ≈ ${usd:>7.2f} ({diff:+.2f}) {flag}")
        print(f"    {'TOTAL':12s}: ≈ ${total_usd:.2f}  (target ${TARGET_USD:.0f}, delta ${total_usd-TARGET_USD:+.2f})")

    print(f"\n  MAIN: { {k:round(v,2) for k,v in main_bals.items()} }")

    # 6. Plan: sells + buys
    print("\n" + "=" * 90)
    print("ACTION PLAN")
    print("=" * 90)

    sells = []  # (pair, sid, base_cur, sell_amt, qcur, est_usd)
    buys = []   # (pair, usd_short, qcur)

    for pair in sorted(pair_inv.keys()):
        info = pair_inv[pair]
        px = prices.get(pair)
        qcur = info['bucket']
        for sid, amt in sorted(info['subs'].items()):
            if qcur == 'ZAR': usd = amt * px / ZAR_USD_REF if px else 0
            else: usd = amt * px if px else 0
            target = TARGET_USD / 2
            if usd > target + 5:
                excess = usd - target
                sell_amt = excess / px if qcur != 'ZAR' else excess * ZAR_USD_REF / px
                sells.append((pair, sid, info['base_cur'], sell_amt, qcur, excess))
                print(f"  SELL {sell_amt:>12.10f} {info['base_cur']} on {SUB_NAMES[sid]} → ~${excess:.2f} {qcur}")
            elif usd < target - 5:
                short = target - usd
                buys.append((pair, short, qcur))
                print(f"  BUY  ${short:.2f} {pair}")

    total_sell = sum(s[5] for s in sells)
    total_buy = sum(b[1] for b in buys)
    print(f"\n  Total sell: ~${total_sell:.2f} | Total buy: ~${total_buy:.2f}")
    print(f"  MAIN quote: { {k:round(v,2) for k,v in main_bals.items() if k in ('ZAR','USDT','USDC')} }")

    if not execute:
        print("\n  *** DRY RUN — pass --execute to run ***")
        return

    # ===== EXECUTION =====
    print("\n>>> EXECUTING <<<")

    # Step 1: SELL excess on subs
    print("\n--- Step 1: Selling excess ---")
    for pair, sid, base_cur, amt, qcur, est in sells:
        print(f"  SELL {amt:.10f} {base_cur} on {SUB_NAMES[sid]} ({pair})...", end=" ")
        status, data = await rest.request('POST', '/v1/orders/market',
            body={'side': 'SELL', 'baseAmount': f'{amt:.10f}', 'pair': pair},
            subaccount_id=sid)
        print(f"status={status}")
        await asyncio.sleep(2)

    print("  Waiting for fills...")
    await asyncio.sleep(45)

    # Step 2: Transfer excess quote to MAIN
    print("\n--- Step 2: Transferring quote to MAIN ---")
    KEEPS = {'ZAR': 2500, 'USDT': 50, 'USDC': 3}
    for bucket, ids in SUB_IDS.items():
        for sid in ids:
            bals = await get_bals(rest, sub_id=sid)
            qcur = bucket
            avail = bals.get(qcur, 0)
            keep = KEEPS.get(qcur, 0)
            excess = avail - keep
            if excess > 1:
                print(f"  Transfer {excess:.4f} {qcur} {SUB_NAMES[sid]} → MAIN...", end=" ")
                s, _ = await rest.request('POST', '/v1/account/subaccounts/transfer',
                    body={'fromId': int(sid), 'toId': 0, 'currencyCode': qcur,
                          'amount': f'{excess:.10f}', 'allowBorrow': False})
                print(f"status={s}")
                await asyncio.sleep(2)
            else:
                print(f"  {SUB_NAMES[sid]}: {avail:.2f} {qcur} (keeping)")

    await asyncio.sleep(30)

    # Step 3: MAIN check + USDT→ZAR if needed
    print("\n--- Step 3: MAIN balances ---")
    main_bals = await get_bals(rest)
    print(f"  MAIN: { {k:round(v,2) for k,v in main_bals.items()} }")

    zar_needed = sum(b[1] for b in buys if b[2] == 'ZAR')
    zar_have = main_bals.get('ZAR', 0)
    usdt_have = main_bals.get('USDT', 0)
    if zar_needed > zar_have and usdt_have > 5:
        cvt = min(usdt_have, (zar_needed - zar_have) * ZAR_USD_REF * 1.1)
        print(f"  Converting {cvt:.2f} USDT → ZAR...", end=" ")
        s, _ = await rest.request('POST', '/v1/orders/market',
            body={'side': 'SELL', 'baseAmount': f'{cvt:.10f}', 'pair': 'USDTZAR'})
        print(f"status={s}")
        await asyncio.sleep(30)
        main_bals = await get_bals(rest)

    # Step 4: Buy thin pairs on MAIN
    print("\n--- Step 4: Buying thin pairs ---")
    for pair, usd_short, qcur in buys:
        spend = usd_short * ZAR_USD_REF if qcur == 'ZAR' else usd_short
        base_cur = pair.replace(qcur, '')
        main_q = main_bals.get(qcur, 0)
        if main_q < spend * 0.5:
            print(f"  SKIP {pair}: MAIN has {main_q:.2f} {qcur}, need {spend:.2f}")
            continue
        print(f"  BUY {spend:.2f} {qcur} of {base_cur}...", end=" ")
        s, _ = await rest.request('POST', '/v1/orders/market',
            body={'side': 'BUY', 'quoteAmount': f'{spend:.4f}', 'pair': pair})
        print(f"status={s}")
        await asyncio.sleep(10)

        # Distribute to subs
        main_bals = await get_bals(rest)
        base_total = main_bals.get(base_cur, 0)
        bucket_ids = SUB_IDS[qcur]
        for sub_sid in bucket_ids:
            half = base_total / 2 * 0.999
            if half > 0:
                print(f"    Transfer {half:.10f} {base_cur} → {SUB_NAMES[sub_sid]}...", end=" ")
                s2, _ = await rest.request('POST', '/v1/account/subaccounts/transfer',
                    body={'fromId': 0, 'toId': int(sub_sid), 'currencyCode': base_cur,
                          'amount': f'{half:.10f}', 'allowBorrow': False})
                print(f"status={s2}")
                await asyncio.sleep(2)

        # Transfer some quote to subs
        main_bals = await get_bals(rest)
        sub_q = main_bals.get(qcur, 0)
        if sub_q > 10:
            for sub_sid in bucket_ids:
                tq = sub_q / 2 * 0.9
                if tq > 5:
                    print(f"    Transfer {tq:.4f} {qcur} → {SUB_NAMES[sub_sid]} (buffer)...", end=" ")
                    s3, _ = await rest.request('POST', '/v1/account/subaccounts/transfer',
                        body={'fromId': 0, 'toId': int(sub_sid), 'currencyCode': qcur,
                              'amount': f'{tq:.4f}', 'allowBorrow': False})
                    print(f"status={s3}")
                    await asyncio.sleep(2)

    # Final
    print("\n--- Final state ---")
    main_bals = await get_bals(rest)
    print(f"  MAIN: { {k:round(v,2) for k,v in main_bals.items()} }")
    for bucket, ids in SUB_IDS.items():
        for sid in ids:
            bals = await get_bals(rest, sub_id=sid)
            q = {k: round(v,2) for k,v in bals.items() if k in ('ZAR','USDT','USDC','EURC')}
            print(f"  {SUB_NAMES[sid]}: {q}")

    print("\n✅ Done! Watch logs for prints resuming.")

if __name__ == '__main__':
    asyncio.run(main(execute='--execute' in sys.argv))
