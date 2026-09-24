#!/usr/bin/env python3
"""Normalize wash-bot inventory to a target $/pair across all 3 buckets.

Fee model: 0.02% taker only. Drain is negligible, so target is a working
buffer, not fee replenishment.

Plan per pair:
  - value = base_units * price_usd  (summed across both subs)
  - if value > target + band  -> SELL excess base on the sub(s) that hold it
  - if value < target - band  -> BUY base on MAIN, split 50/50 to subs
  - else leave

After base normalization, sweep EXCESS quote (ZAR/USDT/USDC) from subs to MAIN,
keeping a working quote buffer.

DRY-RUN by default. Pass --execute to place real market orders + transfers.

Market orders:
  SELL excess base:  POST /v1/orders/market {"side":"SELL","baseAmount":..,"pair":..}  on the SUB
  BUY base on main:  POST /v1/orders/market {"side":"BUY","quoteAmount":..,"pair":..}  on MAIN
Transfers:
  MAIN->sub / sub->MAIN: POST /v1/account/subaccounts/transfer
    {"fromId":..,"toId":..,"currencyCode":..,"amount":..,"allowBorrow":false}
  primary/main fromId = 0
"""
import argparse, asyncio, aiohttp, time, hmac, hashlib, json, sys
from decimal import Decimal, ROUND_DOWN
sys.path.insert(0, "src")
from creds import load

creds = load()
KEY = creds.get("MAIN_API_" + "KEY")
SEC = creds.get("MAIN_API_" + "SECRET")

TARGET = 35.0
BAND = 5.0          # don't act if within +/- $5 of target
QUOTE_BUFFER_PER_PAIR = 15.0  # working quote kept in subs per pair
MIN_ORDER_USD = 2.0  # skip dust trades

MAIN_ID = "0"  # primary account fromId/toId for transfers

SUBS = {
    "CMSZAR1": "1513524239074144256", "CMSZAR2": "1513524288865144832",
    "CMSUSDT1": "1513524297399840768", "CMSUSDT2": "1513524305939443712",
    "CMSUSDC1": "1513524314495823872", "CMSUSDC2": "1513524323040333824",
}

BUCKETS = {
    "ZAR": {
        "subs": ["CMSZAR1", "CMSZAR2"], "quote": "ZAR",
        "pairs": {"BTCZAR":"BTC","ETHZAR":"ETH","XRPZAR":"XRP","SOLZAR":"SOL",
                  "AVAXZAR":"AVAX","BNBZAR":"BNB","LINKZAR":"LINK","XAUTZAR":"XAUT","USDCZAR":"USDC"},
    },
    "USDT": {
        "subs": ["CMSUSDT1", "CMSUSDT2"], "quote": "USDT",
        "pairs": {"XAUTUSDT":"XAUT","SPYXUSDT":"SPYX","NVDAXUSDT":"NVDAX","COINXUSDT":"COINX",
                  "TRUMPUSDT":"TRUMP","MSTRXUSDT":"MSTRX","HOODXUSDT":"HOODX","TSLAXUSDT":"TSLAX",
                  "CRCLXUSDT":"CRCLX","BITGOLDUSDT":"BITGOLD","VALR10USDT":"VALR10","JUPUSDT":"JUP","PUMPUSDT":"PUMP"},
    },
    "USDC": {
        "subs": ["CMSUSDC1", "CMSUSDC2"], "quote": "USDC",
        "pairs": {"EURCUSDC":"EURC"},
    },
}


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
    url = f"https://api.valr.com{path}"
    async with s.request(method, url, headers=headers, data=body or None) as r:
        txt = await r.text()
        try:
            return r.status, json.loads(txt)
        except Exception:
            return r.status, txt


async def balances(s, sub_id):
    st, d = await _req(s, "GET", "/v1/account/balances", sub=sub_id)
    if isinstance(d, list):
        return {it["currency"]: Decimal(str(it.get("available", "0") or "0")) for it in d}
    return {}


async def price_usd(s, pair, quote, zar_usd):
    st, d = await _req(s, "GET", f"/v1/public/{pair}/marketsummary", signed=False)
    p = Decimal(str(d.get("lastTradedPrice", "0") or "0")) if isinstance(d, dict) else Decimal(0)
    if quote == "ZAR":
        return p * Decimal(str(zar_usd))
    return p  # USDT/USDC ~ USD


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--bucket", default="ALL", help="ZAR|USDT|USDC|ALL")
    args = ap.parse_args()

    async with aiohttp.ClientSession() as s:
        # ZAR->USD from USDCZAR (USDC ~ $1)
        st, d = await _req(s, "GET", "/v1/public/USDCZAR/marketsummary", signed=False)
        usdczar = Decimal(str(d.get("lastTradedPrice", "0")))
        zar_usd = float(1 / usdczar)
        print(f"# ZAR/USD = {zar_usd:.5f}  (USDCZAR={usdczar})")
        print(f"# Target ${TARGET}/pair, band +/-${BAND}, min order ${MIN_ORDER_USD}")
        print(f"# Mode: {'EXECUTE' if args.execute else 'DRY-RUN'}\n")

        # cache balances per sub
        bal = {}
        for name, sid in SUBS.items():
            bal[name] = await balances(s, sid)
            await asyncio.sleep(0.12)

        plan_sell = []  # (sub_name, sub_id, pair, base, base_amount, usd)
        plan_buy = []   # (pair, base, quote_usd)
        sweep = {}      # quote_currency -> usd to sweep

        for bname, bk in BUCKETS.items():
            if args.bucket != "ALL" and args.bucket != bname:
                continue
            print(f"{'='*60}\n{bname} BUCKET\n{'='*60}")
            quote = bk["quote"]
            for pair, base in bk["pairs"].items():
                px = await price_usd(s, pair, quote, zar_usd)
                await asyncio.sleep(0.10)
                if px <= 0:
                    print(f"  {pair}: price fetch failed, skip")
                    continue
                units = sum(bal[sub].get(base, Decimal(0)) for sub in bk["subs"])
                val = float(units * px)
                if val > TARGET + BAND:
                    excess_usd = val - TARGET
                    # sell proportionally from whichever subs hold base
                    for sub in bk["subs"]:
                        sub_units = bal[sub].get(base, Decimal(0))
                        sub_val = float(sub_units * px)
                        # target per sub = TARGET/2
                        sub_excess = sub_val - TARGET / 2
                        if sub_excess > MIN_ORDER_USD:
                            sell_units = (Decimal(str(sub_excess)) / px)
                            plan_sell.append((sub, SUBS[sub], pair, base, sell_units, sub_excess))
                    print(f"  {pair:<12} ${val:>8.2f}  SELL ${excess_usd:.0f} excess")
                elif val < TARGET - BAND:
                    deficit = TARGET - val
                    plan_buy.append((pair, base, deficit))
                    print(f"  {pair:<12} ${val:>8.2f}  BUY  ${deficit:.0f}")
                else:
                    print(f"  {pair:<12} ${val:>8.2f}  ok")

        print(f"\n{'='*60}\nSUMMARY\n{'='*60}")
        tot_sell = sum(x[5] for x in plan_sell)
        tot_buy = sum(x[2] for x in plan_buy)
        print(f"SELL orders: {len(plan_sell)}  total ≈ ${tot_sell:.0f}")
        print(f"BUY orders:  {len(plan_buy)}  total ≈ ${tot_buy:.0f}")
        print(f"Net freed (sell - buy) ≈ ${tot_sell - tot_buy:.0f}\n")

        print("--- SELL plan (on subs) ---")
        for sub, sid, pair, base, amt, usd in plan_sell:
            print(f"  {sub} SELL {amt:.8f} {base} ({pair}) ≈ ${usd:.0f}")
        print("\n--- BUY plan (on MAIN, then split 50/50) ---")
        for pair, base, usd in plan_buy:
            print(f"  MAIN BUY ${usd:.0f} {base} ({pair}) -> split to subs")

        if not args.execute:
            print("\n[DRY-RUN] No orders placed. Re-run with --execute to act.")
            return

        # ---- EXECUTE ----
        print("\n[EXECUTE] placing market orders...")
        # 1. SELL excess base on subs
        for sub, sid, pair, base, amt, usd in plan_sell:
            # round base amount down to reasonable precision
            amt_str = str(amt.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN))
            body = json.dumps({"side": "SELL", "baseAmount": amt_str, "pair": pair})
            st, d = await _req(s, "POST", "/v1/orders/market", body=body, sub=sid)
            print(f"  SELL {sub} {amt_str} {base} {pair}: {st} {d}")
            await asyncio.sleep(0.3)

        print("\n[EXECUTE] BUY plan requires MAIN-account quote routing per bucket.")
        print("  (run buy/transfer phase separately after confirming sells settled)")

asyncio.run(main())
