#!/usr/bin/env python3
"""Restock thin/degraded wash-bot pairs by buying on MAIN and splitting 50/50 to subs.

DRY-RUN by default. Pass --execute to actually place orders.
"""
import argparse, asyncio, aiohttp, time, hmac, hashlib, json, sys, re
sys.path.insert(0, "src")
from creds import load

creds = load()
KEY = creds.get("MAIN_API_" + "KEY")
SEC = creds.get("MAIN_API_" + "SECRET")

MAIN_ID = "0"
SUB_IDS = {
    "zar":  ("1513524239074144256", "1513524288865144832"),   # CMSZAR1, CMSZAR2
    "usdt": ("1513524297394840768", "1513524305939443712"),   # CMSUSDT1, CMSUSDT2
    "usdc": ("1513524314495823872", "1513524323040333824"),   # USDC1, USDC2
}

ZAR_REF = 16.64

# Thin pairs: (pair, quoteAmount, bucket)
PAIRS = [
    ("AVAXZAR",   "90",     "zar"),
    ("BNBZAR",    "90",     "zar"),
    ("ETHZAR",    "90",     "zar"),
    ("LINKZAR",   "90",     "zar"),
    ("SOLZAR",    "90",     "zar"),
    ("USDCZAR",   "90",     "zar"),
    ("XAUTZAR",   "90",     "zar"),
    ("XRPZAR",    "90",     "zar"),
    ("XAUTUSDT",  "35",     "usdt"),
]

ZAR_NEEDED = 750  # R750 buffer for 8 ZAR pairs
BASE_URL = "https://api.valr.com"


def sign(method, path, body="", subaccountId=None):
    ts = str(int(time.time() * 1000))
    payload = ts + method + path + body
    if subaccountId:
        payload += subaccountId
    sig = hmac.new(SEC.encode(), payload.encode(), hashlib.sha512).hexdigest()
    return ts, sig


def extract_base_currency(pair):
    """Extract the base currency from a pair string like 'AVAXZAR' -> 'AVAX'."""
    # Try known quote currencies
    for q in ["USDC", "USDT", "ZAR", "EURC"]:
        if pair.endswith(q):
            return pair[:-len(q)]
    # Fallback: strip last 3 chars
    return pair[:-3]


async def get_main_balances(session):
    path = "/v1/account/balances"
    ts, sig = sign("GET", path)
    headers = {"X-VALR-API-KEY": KEY, "X-VALR-TIMESTAMP": ts, "X-VALR-SIGNATURE": sig}
    async with session.get(BASE_URL + path, headers=headers) as r:
        bals = await r.json()
    # API returns a list directly
    result = {}
    for b in bals:
        result[b["currency"]] = float(b["available"])
    return result


async def market_order(session, side, pair, quoteAmount=None, baseAmount=None, subaccountId=None):
    path = "/v1/orders/market"
    body = {"side": side, "pair": pair}
    if quoteAmount is not None:
        body["quoteAmount"] = str(quoteAmount)
    if baseAmount is not None:
        body["baseAmount"] = str(baseAmount)
    body_json = json.dumps(body)
    headers = {"X-VALR-API-KEY": KEY, "Content-Type": "application/json"}
    if subaccountId:
        headers["X-VALR-SUB-ACCOUNT-ID"] = subaccountId
        ts, sig = sign("POST", path, body_json, subaccountId=subaccountId)
    else:
        ts, sig = sign("POST", path, body_json)
    headers["X-VALR-TIMESTAMP"] = ts
    headers["X-VALR-SIGNATURE"] = sig
    async with session.post(BASE_URL + path, headers=headers, data=body_json) as r:
        result = await r.json()
        if r.status != 200:
            print(f"  ❌ {side} {pair}: HTTP {r.status} {result}")
            return None
        oid = result.get("orderId", "?")
        qf = result.get("quoteFilled", "?")
        bf = result.get("baseFilled", "?")
        print(f"  ✅ {side} {pair}: orderId={oid} quoteFilled={qf} baseFilled={bf}")
        return result


async def transfer(session, from_id, to_id, currency, amount):
    path = "/v1/account/subaccounts/transfer"
    body = {"fromId": int(from_id), "toId": int(to_id), "currencyCode": currency, "amount": str(amount), "allowBorrow": False}
    body_json = json.dumps(body)
    headers = {"X-VALR-API-KEY": KEY, "Content-Type": "application/json"}
    ts, sig = sign("POST", path, body_json)
    headers["X-VALR-TIMESTAMP"] = ts
    headers["X-VALR-SIGNATURE"] = sig
    async with session.post(BASE_URL + path, headers=headers, data=body_json) as r:
        result = await r.json()
        if r.status != 200:
            print(f"    ❌ transfer {currency} {amount} {from_id}→{to_id}: HTTP {r.status} {result}")
            return None
        print(f"    ✅ transfer {currency} {amount} {from_id}→{to_id}")
        return result


async def get_price(session, pair):
    path = f"/v1/public/{pair}/marketsummary"
    async with session.get(BASE_URL + path) as r:
        data = await r.json()
        return float(data.get("lastTradedPrice", 0))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Actually execute orders (default: dry-run)")
    args = parser.parse_args()
    mode = "🔴 EXECUTE" if args.execute else "🟡 DRY-RUN"
    print(f"=== Restock thin pairs [{mode}] ===")
    print(f"ZAR/USD ref: {ZAR_REF}")

    async with aiohttp.ClientSession() as s:
        # 1. Check MAIN balances
        bals = await get_main_balances(s)
        zar_bal = bals.get("ZAR", 0)
        usdt_bal = bals.get("USDT", 0)
        print(f"\nMAIN balances: ZAR={zar_bal:.2f}, USDT={usdt_bal:.2f}")

        # 2. Convert USDT→ZAR if needed
        if zar_bal < ZAR_NEEDED:
            needed_zar = ZAR_NEEDED - zar_bal
            usdt_to_sell = needed_zar / ZAR_REF + 5  # +5 buffer
            print(f"\n⚡ Need R{needed_zar:.0f} more ZAR → selling ~{usdt_to_sell:.2f} USDT on USDTZAR")
            if args.execute:
                result = await market_order(s, "SELL", "USDTZAR", baseAmount=str(round(usdt_to_sell, 2)))
                if result:
                    print("  ⏳ Waiting 10s for balance to settle...")
                    await asyncio.sleep(10)
            else:
                print(f"  [DRY-RUN] would SELL {usdt_to_sell:.2f} USDT on USDTZAR")

        # 3. Show plan
        print(f"\n📊 Plan:")
        for pair, quote_amt, bucket in PAIRS:
            try:
                price = await get_price(s, pair)
                est_base = float(quote_amt) / price
                print(f"  {pair:12s} | quoteAmt={quote_amt} | price≈{price} | est_base≈{est_base:.6f}")
            except:
                print(f"  {pair:12s} | quoteAmt={quote_amt} | price=?")

        total_zar_spend = sum(float(q) for _, q, b in PAIRS if b == "zar")
        total_usdt_spend = sum(float(q) for _, q, b in PAIRS if b == "usdt")
        print(f"\n  Total ZAR spend: ~R{total_zar_spend:.0f}  (≈${total_zar_spend/ZAR_REF:.0f})")
        print(f"  Total USDT spend: ~${total_usdt_spend}")
        print(f"  Grand total: ≈${total_zar_spend/ZAR_REF + total_usdt_spend:.0f}")

        # 4. Buy each pair on MAIN + transfer
        for pair, quote_amt, bucket in PAIRS:
            base_curr = extract_base_currency(pair)
            print(f"\n🛒 BUY {pair} on MAIN (quoteAmount={quote_amt})")
            if args.execute:
                result = await market_order(s, "BUY", pair, quoteAmount=quote_amt)
                if result:
                    base_filled = float(result.get("baseFilled", 0))
                    if base_filled > 0:
                        sub_a, sub_b = SUB_IDS[bucket]
                        half = base_filled / 2
                        print(f"  → Splitting {base_filled:.8f} {base_curr} 50/50 to subs:")
                        await transfer(s, MAIN_ID, sub_a, base_curr, f"{half:.8f}")
                        await transfer(s, MAIN_ID, sub_b, base_curr, f"{half:.8f}")
                    await asyncio.sleep(2)  # rate limit spacing
            else:
                print(f"  [DRY-RUN] would BUY {pair} quoteAmount={quote_amt} → transfer {base_curr} 50/50")

        print(f"\n{'='*50}")
        if not args.execute:
            print("This was a DRY-RUN. Pass --execute to actually run orders.")


if __name__ == "__main__":
    asyncio.run(main())
