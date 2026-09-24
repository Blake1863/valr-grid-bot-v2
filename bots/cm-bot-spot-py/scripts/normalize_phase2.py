#!/usr/bin/env python3
"""Phase 2: sweep excess quote sub->MAIN, buy base for thin pairs, split to subs.

Fee model: 0.02% taker only.
Run order:
  1. Sweep excess quote (ZAR/USDT/USDC) from subs -> MAIN, keep working buffer.
  2. Buy base on MAIN (quoteAmount market buys) for thin pairs.
  3. Split bought base 50/50 to the two subs of that bucket.

DRY-RUN by default. --execute to act.
"""
import argparse, asyncio, aiohttp, time, hmac, hashlib, json, sys
from decimal import Decimal, ROUND_DOWN
sys.path.insert(0, "src")
from creds import load
c = load()
KEY = c.get("MAIN_API_" + "KEY"); SEC = c.get("MAIN_API_" + "SECRET")

SUBS = {
    "CMSZAR1": "1513524239074144256", "CMSZAR2": "1513524288865144832",
    "CMSUSDT1": "1513524297399840768", "CMSUSDT2": "1513524305939443712",
    "CMSUSDC1": "1513524314495823872", "CMSUSDC2": "1513524323040333824",
}
MAIN_ID = "0"

# Working quote buffer to LEAVE in each sub (total per bucket)
# ZAR: 9 pairs need a little ZAR each. Keep ~R2500/sub. USDT: keep ~$50/sub. USDC: keep ~$15/sub.
KEEP = {"ZAR": Decimal("2500"), "USDT": Decimal("50"), "USDC": Decimal("3")}

# Base buys on MAIN: pair -> (quote_currency, usd_target_buy)
BUYS = {
    "ETHZAR":   ("ZAR", Decimal("32")),
    "SOLZAR":   ("ZAR", Decimal("29")),
    "XAUTUSDT": ("USDT", Decimal("19")),
    "EURCUSDC": ("USDC", Decimal("20")),
}
BUCKET_SUBS = {
    "ZAR": ["CMSZAR1", "CMSZAR2"],
    "USDT": ["CMSUSDT1", "CMSUSDT2"],
    "USDC": ["CMSUSDC1", "CMSUSDC2"],
}
PAIR_BASE = {"ETHZAR": "ETH", "SOLZAR": "SOL", "XAUTUSDT": "XAUT", "EURCUSDC": "EURC"}


async def _req(s, method, path, body="", signed=True, sub=None):
    ts = str(int(time.time() * 1000))
    headers = {}
    if signed:
        payload = ts + method + path + body + (sub or "")
        sig = hmac.new(SEC.encode(), payload.encode(), hashlib.sha512).hexdigest()
        headers = {"X-VALR-API-KEY": KEY, "X-VALR-SIGNATURE": sig, "X-VALR-TIMESTAMP": ts}
        if sub:
            headers["X-VALR-SUB-ACCOUNT-ID"] = sub
    if body:
        headers["Content-Type"] = "application/json"
    async with s.request(method, f"https://api.valr.com{path}", headers=headers, data=body or None) as r:
        t = await r.text()
        try:
            return r.status, json.loads(t)
        except Exception:
            return r.status, t


async def bal(s, sid, cur):
    st, d = await _req(s, "GET", "/v1/account/balances", sub=sid)
    if isinstance(d, list):
        for it in d:
            if it["currency"] == cur:
                return Decimal(str(it.get("available", "0") or "0"))
    return Decimal(0)


async def main_bal(s, cur):
    st, d = await _req(s, "GET", "/v1/account/balances")
    if isinstance(d, list):
        for it in d:
            if it["currency"] == cur:
                return Decimal(str(it.get("available", "0") or "0"))
    return Decimal(0)


async def fx_zar(s):
    st, d = await _req(s, "GET", "/v1/public/USDCZAR/marketsummary", signed=False)
    return Decimal(str(d["lastTradedPrice"]))  # ZAR per USD (USDC~$1)


async def transfer(s, from_id, to_id, cur, amount, execute):
    body = json.dumps({"fromId": from_id, "toId": to_id, "currencyCode": cur,
                       "amount": str(amount), "allowBorrow": False})
    if not execute:
        print(f"    [dry] TRANSFER {cur} {amount} {from_id}->{to_id}")
        return 0, "dry"
    st, d = await _req(s, "POST", "/v1/account/subaccounts/transfer", body=body)
    print(f"    TRANSFER {cur} {amount} {from_id}->{to_id}: {st} {d}")
    return st, d


async def market_buy(s, pair, quote_amount, execute):
    body = json.dumps({"side": "BUY", "quoteAmount": str(quote_amount), "pair": pair})
    if not execute:
        print(f"    [dry] MAIN BUY {pair} quote={quote_amount}")
        return 0, "dry"
    st, d = await _req(s, "POST", "/v1/orders/market", body=body)
    print(f"    MAIN BUY {pair} quote={quote_amount}: {st} {d}")
    return st, d


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    ex = args.execute

    async with aiohttp.ClientSession() as s:
        zar_per_usd = await fx_zar(s)
        print(f"# FX: {zar_per_usd} ZAR/USD | Mode: {'EXECUTE' if ex else 'DRY-RUN'}\n")

        # ---- 1. SWEEP excess quote sub->MAIN ----
        print("=== STEP 1: sweep excess quote sub -> MAIN ===")
        for bucket, subs in BUCKET_SUBS.items():
            keep = KEEP[bucket]
            for subname in subs:
                sid = SUBS[subname]
                q = await bal(s, sid, bucket)
                excess = q - keep
                if excess > Decimal("1"):
                    excess = excess.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
                    print(f"  {subname}: {bucket} avail={q}, keep={keep}, sweep {excess}")
                    await transfer(s, sid, MAIN_ID, bucket, excess, ex)
                    await asyncio.sleep(0.3)
                else:
                    print(f"  {subname}: {bucket} avail={q}, nothing to sweep")

        # ---- 2. BUY base on MAIN ----
        print("\n=== STEP 2: buy base on MAIN for thin pairs ===")
        # need quote on MAIN. For ZAR buys, the sweep above provided ZAR.
        bought = {}
        for pair, (quote, usd) in BUYS.items():
            if quote == "ZAR":
                qa = (usd * zar_per_usd).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            else:
                qa = usd.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            mb = await main_bal(s, quote)
            print(f"  {pair}: need {quote} {qa} (MAIN has {mb})")
            if mb < qa:
                print(f"    !! insufficient {quote} on MAIN, skipping {pair}")
                continue
            st, d = await market_buy(s, pair, qa, ex)
            bought[pair] = (PAIR_BASE[pair], quote)
            await asyncio.sleep(0.4)

        # ---- 3. split bought base 50/50 to subs ----
        print("\n=== STEP 3: split bought base 50/50 to subs ===")
        if not ex:
            print("  [dry] (would read MAIN base balance and split each)")
        for pair, (base, quote) in bought.items():
            bucket = quote
            subs = BUCKET_SUBS[bucket]
            mb = await main_bal(s, base)
            half = (mb / 2).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            print(f"  {pair}: MAIN {base}={mb}, split {half} to each of {subs}")
            if half > 0:
                await transfer(s, MAIN_ID, SUBS[subs[0]], base, half, ex)
                await asyncio.sleep(0.3)
                # send remainder to second sub
                mb2 = await main_bal(s, base)
                send2 = mb2.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
                await transfer(s, MAIN_ID, SUBS[subs[1]], base, send2, ex)
                await asyncio.sleep(0.3)

        if not ex:
            print("\n[DRY-RUN] nothing executed. Re-run with --execute.")

asyncio.run(main())
