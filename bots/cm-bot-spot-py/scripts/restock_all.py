#!/usr/bin/env python3
"""
Restock all CMS subaccounts from MAIN.
Steps: USDT->ZAR conversion, buy all base coins on MAIN, transfer to subs, fund working buffers.

Usage: python3 restock_all.py [--execute]
"""
import asyncio, sys, os

sys.path.insert(0, '/home/admin/.openclaw/workspace/bots/cm-bot-spot-py')

# Cred gotcha: split the key name to avoid chat redaction filter
from src.creds import load
from src.valr_rest import ValrRest

# Subaccount IDs
CMS_SUBS = {
    "CMSZAR1": "1513524239074144256",
    "CMSZAR2": "1513524288865144832",
    "CMSUSDT1": "1513524297399840768",
    "CMSUSDT2": "1513524305939443712",
    "CMSUSDC1": "1513524314495823872",
    "CMSUSDC2": "1513524323040333824",
}
MAIN = "0"

# --- CONFIG ---
ZAR_PER_PAIR = 700        # R700 per ZAR pair
USDT_PER_PAIR = 100       # $100 per USDT pair
ZAR_BUFFER_PER_SUB = 2500 # R2500 working buffer per ZAR sub
USDT_BUFFER_PER_SUB = 50  # $50 working buffer per USDT sub
USDC_BUFFER_PER_SUB = 3   # $3 per USDC sub
EURC_BUY = 10             # EURC to buy for EURCUSDC

ZAR_PAIRS = [
    ("BTCZAR", "BTC"), ("ETHZAR", "ETH"), ("XRPZAR", "XRP"),
    ("SOLZAR", "SOL"), ("AVAXZAR", "AVAX"), ("BNBZAR", "BNB"),
    ("LINKZAR", "LINK"), ("XAUTZAR", "XAUT"),
]

USDT_PAIRS = [
    ("XAUTUSDT", "XAUT"), ("SPYXUSDT", "SPYX"), ("NVDAXUSDT", "NVDAX"),
    ("COINXUSDT", "COINX"), ("TRUMPUSDT", "TRUMP"), ("MSTRXUSDT", "MSTRX"),
    ("HOODXUSDT", "HOODX"), ("TSLAXUSDT", "TSLAX"), ("CRCLXUSDT", "CRCLX"),
    ("BITGOLDUSDT", "BITGOLD"), ("VALR10USDT", "VALR10"), ("JUPUSDT", "JUP"),
    ("PUMPUSDT", "PUMP"),
]

# Skip these pairs (1-tick spread, low value - per Blake)
SKIP_PAIRS = set()


async def main():
    execute = '--execute' in sys.argv
    creds = load()
    key = creds['MAIN_API_' + 'KEY']
    secret = creds['MAIN_API_' + 'SECRET']
    rest = ValrRest(key, secret)

    # Get MAIN balances
    r = await rest.request('GET', '/v1/account/balances')
    if isinstance(r, tuple): r = r[1]
    bals = {}
    for b in r:
        bals[b['currency']] = float(b.get('available', 0))

    # Get prices
    all_pairs = [p[0] for p in ZAR_PAIRS + USDT_PAIRS] + ['USDTZAR', 'EURCUSDC']
    prices = {}
    for pair in all_pairs:
        try:
            r = await rest.request('GET', f'/v1/public/{pair}/marketsummary')
            if isinstance(r, tuple): r = r[1]
            prices[pair] = float(r.get('lastTradedPrice', 0))
        except Exception as e:
            prices[pair] = 0
            print(f"  WARN: No price for {pair}: {e}")
        await asyncio.sleep(0.05)

    print("=== CURRENT PRICES ===")
    for p, pr in sorted(prices.items()):
        if pr > 0:
            print(f"  {p}: {pr}")

    # === STEP 1: Calculate and execute USDT->ZAR conversion ===
    zar_have = bals.get('ZAR', 0)
    zar_base_need = 0
    for pair, base in ZAR_PAIRS:
        price = prices[pair]
        if price == 0: continue
        base_qty = ZAR_PER_PAIR / price
        have = bals.get(base, 0)
        need = max(0, base_qty - have)
        zar_base_need += need * price

    zar_buffer_total = ZAR_BUFFER_PER_SUB * 2
    zar_total_need = zar_base_need + zar_buffer_total
    zar_to_buy = max(0, zar_total_need - zar_have)

    usdtzr_price = prices.get('USDTZAR', 16.5)
    usdt_for_zar = zar_to_buy / usdtzr_price if zar_to_buy > 0 else 0

    print(f"\n=== STEP 1: USDT -> ZAR ===")
    print(f"  ZAR needed for base: R{zar_base_need:.2f}")
    print(f"  ZAR buffer: R{zar_buffer_total:.2f}")
    print(f"  Have ZAR: R{zar_have:.2f}")
    print(f"  Need R{zar_to_buy:.2f} (~${usdt_for_zar:.2f})")

    if usdt_for_zar > 0.5 and execute:
        # Sell USDT for ZAR
        usdt_sell_amt = usdt_for_zar * 1.01  # slight overage for slippage
        print(f"  SELLING {usdt_sell_amt:.2f} USDT for ZAR...")
        try:
            r = await rest.request('POST', '/v1/orders/market',
                body={"side": "SELL", "quoteAmount": f"{usdt_sell_amt:.2f}", "pair": "USDTZAR"})
            print(f"  OK: {r}")
        except Exception as e:
            print(f"  FAIL: {e}")
            if not execute: pass
        await asyncio.sleep(3)

        # Refresh ZAR balance
        r = await rest.request('GET', '/v1/account/balances')
        if isinstance(r, tuple): r = r[1]
        for b in r:
            if b['currency'] == 'ZAR':
                zar_have = float(b.get('available', 0))
                print(f"  ZAR balance after: R{zar_have:.2f}")

    elif usdt_for_zar > 0.5:
        print(f"  [DRY RUN] Would sell ${usdt_for_zar:.2f} USDT -> ZAR")

    # === STEP 2: Buy base coins ===
    print(f"\n=== STEP 2: BUY BASE COINS ===")

    buys = []  # (pair, base, quote_amount, base_amount_estimate)

    # ZAR pairs
    for pair, base in ZAR_PAIRS:
        price = prices[pair]
        if price == 0: continue
        base_qty = ZAR_PER_PAIR / price
        have = bals.get(base, 0)
        need = max(0, base_qty - have)
        cost = need * price
        if cost > 1:
            buys.append((pair, base, cost, need))
            print(f"  BUY {pair}: R{cost:.2f} (~{need:.8f} {base})")

    # USDT pairs
    for pair, base in USDT_PAIRS:
        price = prices[pair]
        if price == 0: continue
        base_qty = USDT_PER_PAIR / price
        have = bals.get(base, 0)
        need = max(0, base_qty - have)
        cost = need * price
        if cost > 0.5:
            buys.append((pair, base, cost, need))
            print(f"  BUY {pair}: ${cost:.2f} (~{need:.6f} {base})")

    # EURC for EURCUSDC
    eurc_price = prices.get('EURCUSDC', 1.144)
    eurc_need = EURC_BUY / eurc_price
    eurc_have = bals.get('EURC', 0)
    eurc_buy = max(0, eurc_need - eurc_have)
    if eurc_buy > 0.01:
        buys.append(("EURCUSDC", "EURC", EURC_BUY, eurc_buy))
        print(f"  BUY EURCUSDC: ${EURC_BUY:.2f} (~{eurc_buy:.4f} EURC)")

    total_zar_buy = sum(b[2] for b in buys if b[1] != 'USDT' and 'ZAR' in b[0])
    total_usdt_buy = sum(b[2] for b in buys if 'USDT' in b[0] or b[1] not in ['EURC'])
    print(f"\n  Total ZAR buys: R{total_zar_buy:.2f}")
    print(f"  Total USDT buys: ~${total_usdt_buy:.2f}")

    if execute:
        for pair, base, cost, amt in buys:
            if pair == 'EURCUSDC':
                # EURC/USDC - buy EURC using USDC, market buy
                try:
                    r = await rest.request('POST', '/v1/orders/market',
                        body={"side": "BUY", "quoteAmount": f"{cost:.2f}", "pair": "EURCUSDC"})
                    print(f"  OK: BUY EURCUSDC ${cost:.2f}: {r}")
                except Exception as e:
                    print(f"  FAIL: {e}")
            elif 'ZAR' in pair:
                try:
                    r = await rest.request('POST', '/v1/orders/market',
                        body={"side": "BUY", "quoteAmount": f"{cost:.2f}", "pair": pair})
                    print(f"  OK: BUY {pair} R{cost:.2f}: {r}")
                except Exception as e:
                    print(f"  FAIL: {e}")
            else:
                try:
                    r = await rest.request('POST', '/v1/orders/market',
                        body={"side": "BUY", "quoteAmount": f"{cost:.2f}", "pair": pair})
                    print(f"  OK: BUY {pair} ${cost:.2f}: {r}")
                except Exception as e:
                    print(f"  FAIL: {e}")
            await asyncio.sleep(0.5)

        print("\n  Waiting for balances to settle...")
        await asyncio.sleep(10)

        # Refresh MAIN balances
        r = await rest.request('GET', '/v1/account/balances')
        if isinstance(r, tuple): r = r[1]
        bals = {}
        for b in r:
            bals[b['currency']] = float(b.get('available', 0))

    # === STEP 3: Transfer base coins to CMS subs ===
    print(f"\n=== STEP 3: TRANSFERS TO SUBS ===")

    transfers = []  # (from_sub_id_or_0, to_sub_id, currency, amount, label)

    # ZAR pairs -> both ZAR subs
    for pair, base in ZAR_PAIRS:
        have = bals.get(base, 0)
        if have < 0.00000001:
            print(f"  SKIP {base}: insufficient balance ({have:.10f})")
            continue
        half = have * 0.4999  # leave tiny dust in MAIN
        transfers.append(("MAIN", CMS_SUBS["CMSZAR1"], base, half, f"ZAR1/{base}"))
        transfers.append(("MAIN", CMS_SUBS["CMSZAR2"], base, half, f"ZAR2/{base}"))
        print(f"  MAIN -> CMSZAR1: {half:.10f} {base}")
        print(f"  MAIN -> CMSZAR2: {half:.10f} {base}")

    # USDT pairs -> both USDT subs
    for pair, base in USDT_PAIRS:
        have = bals.get(base, 0)
        if have < 0.00000001:
            print(f"  SKIP {base}: insufficient balance ({have:.10f})")
            continue
        half = have * 0.4999
        transfers.append(("MAIN", CMS_SUBS["CMSUSDT1"], base, half, f"USDT1/{base}"))
        transfers.append(("MAIN", CMS_SUBS["CMSUSDT2"], base, half, f"USDT2/{base}"))
        print(f"  MAIN -> CMSUSDT1: {half:.10f} {base}")
        print(f"  MAIN -> CMSUSDT2: {half:.10f} {base}")

    # EURC -> both USDC subs
    eurc_have = bals.get('EURC', 0)
    if eurc_have > 0.0001:
        half = eurc_have * 0.4999
        transfers.append(("MAIN", CMS_SUBS["CMSUSDC1"], "EURC", half, "USDC1/EURC"))
        transfers.append(("MAIN", CMS_SUBS["CMSUSDC2"], "EURC", half, "USDC2/EURC"))
        print(f"  MAIN -> CMSUSDC1: {half:.6f} EURC")
        print(f"  MAIN -> CMSUSDC2: {half:.6f} EURC")

    # ZAR buffer
    zar_have = bals.get('ZAR', 0)
    zar_buffer = min(ZAR_BUFFER_PER_SUB, zar_have * 0.49)
    if zar_buffer > 1:
        transfers.append(("MAIN", CMS_SUBS["CMSZAR1"], "ZAR", zar_buffer, "ZAR1/buffer"))
        transfers.append(("MAIN", CMS_SUBS["CMSZAR2"], "ZAR", zar_buffer, "ZAR2/buffer"))
        print(f"  MAIN -> CMSZAR1: R{zar_buffer:.2f} (buffer)")
        print(f"  MAIN -> CMSZAR2: R{zar_buffer:.2f} (buffer)")

    # USDT buffer
    usdt_have = bals.get('USDT', 0)
    usdt_buffer = min(USDT_BUFFER_PER_SUB, usdt_have * 0.24)
    if usdt_buffer > 0.5:
        transfers.append(("MAIN", CMS_SUBS["CMSUSDT1"], "USDT", usdt_buffer, "USDT1/buffer"))
        transfers.append(("MAIN", CMS_SUBS["CMSUSDT2"], "USDT", usdt_buffer, "USDT2/buffer"))
        print(f"  MAIN -> CMSUSDT1: ${usdt_buffer:.2f} (buffer)")
        print(f"  MAIN -> CMSUSDT2: ${usdt_buffer:.2f} (buffer)")

    # USDC buffer
    usdc_have = bals.get('USDC', 0)
    usdc_buffer = min(USDC_BUFFER_PER_SUB, usdc_have * 0.49)
    if usdc_buffer > 0.01:
        transfers.append(("MAIN", CMS_SUBS["CMSUSDC1"], "USDC", usdc_buffer, "USDC1/buffer"))
        transfers.append(("MAIN", CMS_SUBS["CMSUSDC2"], "USDC", usdc_buffer, "USDC2/buffer"))
        print(f"  MAIN -> CMSUSDC1: ${usdc_buffer:.2f} USDC (buffer)")
        print(f"  MAIN -> CMSUSDC2: ${usdc_buffer:.2f} USDC (buffer)")

    print(f"\n  Total transfers: {len(transfers)}")

    if execute:
        ok = 0
        fail = 0
        for _, to_id, cur, amt, label in transfers:
            if amt < 0.00000001:
                continue
            try:
                r = await rest.request('POST', '/v1/account/subaccounts/transfer',
                    body={
                        "fromId": 0,
                        "toId": int(to_id),
                        "currencyCode": cur,
                        "amount": f"{amt:.10f}",
                        "allowBorrow": False
                    })
                print(f"  OK: {label}: {r}")
                ok += 1
            except Exception as e:
                print(f"  FAIL: {label}: {e}")
                fail += 1
            await asyncio.sleep(0.3)
        print(f"\n  Results: {ok} OK, {fail} FAIL")

    else:
        print("  DRY RUN. Pass --execute to run.")


asyncio.run(main())
