#!/usr/bin/env python3
"""
rebuild_quote_buffers.py — Fix quote-starved CMS subs by selling base ON the subs.

Root cause: external fills drain quote working buffers. Subs accumulate base
but can't place buy orders because they have no quote. This script:
1. Detects subs with quote buffer below target
2. Market-sells excess base on that sub to build quote
3. No MAIN involvement, no transfers needed

Usage:
  python3 scripts/rebuild_quote_buffers.py              # dry-run
  python3 scripts/rebuild_quote_buffers.py --execute     # actually sell

Targets (per sub):
  ZAR subs:  R2500 quote buffer
  USDT subs: $50   quote buffer
  USDC subs: $3    quote buffer

Safety:
  - Only sells base from pairs that are enabled in the bucket config
  - Caps sell at 50% of each base holding (keep half for sell-side orders)
  - Skips pairs with 1-tick spread (SPYXUSDT, EURCUSDC)
"""
from __future__ import annotations

import asyncio
import aiohttp
import hmac
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.creds import load

creds = load()
KEY = creds["MAIN_API_" + "KEY"]
SEC = creds["MAIN_API_" + "SECRET"]

# Sub account definitions
BUCKETS = {
    "zar": {
        "subs": {
            "CMSZAR1": "1513524239074144256",
            "CMSZAR2": "1513524288865144832",
        },
        "quote": "ZAR",
        "quote_target": 2500,
        "quote_symbol": "R",
        "config": "config-zar.json",
    },
    "usdt": {
        "subs": {
            "CMSUSDT1": "1513524297399840768",
            "CMSUSDT2": "1513524305939443712",
        },
        "quote": "USDT",
        "quote_target": 50,
        "quote_symbol": "$",
        "config": "config-usdt.json",
    },
    "usdc": {
        "subs": {
            "CMSUSDC1": "1513524314495823872",
            "CMSUSDC2": "1513524323040333824",
        },
        "quote": "USDC",
        "quote_target": 3,
        "quote_symbol": "$",
        "config": "config-usdc.json",
    },
    "btc": {
        "subs": {
            "CMSBTC1": "1513524239074144256",  # Reuse CMSZAR1
            "CMSBTC2": "1513524288865144832",  # Reuse CMSZAR2
        },
        "quote": "BTC",
        "quote_target": 0.001,
        "quote_symbol": "₿",
        "config": "config-btc.json",
    },
}

# Pairs that should never be traded (1-tick spread, can't wash)
SKIP_PAIRS = {"SPYXUSDT", "EURCUSDC"}

CONFIG_DIR = Path(__file__).resolve().parent.parent


def enabled_bases(bucket_cfg: dict) -> set[str]:
    """Get set of base currencies for enabled pairs in a bucket."""
    bases = set()
    for pair, pcfg in bucket_cfg.get("pairs", {}).items():
        if not pcfg.get("enabled", True):
            continue
        if pair in SKIP_PAIRS:
            continue
        for q in ("USDT", "USDC", "ZAR", "BTC"):
            if pair.endswith(q):
                bases.add(pair[:-len(q)])
                break
    return bases


async def api(session, method, path, body="", sub=None):
    """Signed VALR API request."""
    ts = str(int(time.time() * 1000))
    payload = ts + method.upper() + path + body + (sub or "")
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
        except Exception:
            return r.status, {"raw": txt[:200]}


async def get_balances(session, sub_id):
    """Get balances for a subaccount."""
    st, data = await api(session, "GET", "/v1/account/balances", sub=sub_id)
    if st != 200 or not isinstance(data, list):
        return None
    return {b["currency"]: float(b.get("available", 0)) for b in data}


async def get_price(session, pair):
    """Get last traded price for a pair."""
    st, data = await api(session, "GET", f"/v1/public/{pair}/marketsummary")
    if st != 200 or not isinstance(data, dict):
        return None
    return float(data.get("lastTradedPrice", 0) or data.get("markPrice", 0) or 0)


async def market_sell(session, sub_id, pair, base_amount):
    """Market sell base_amount of base currency on pair in subaccount."""
    body = json.dumps({"side": "SELL", "baseAmount": str(base_amount), "pair": pair})
    st, data = await api(session, "POST", "/v1/orders/market", body=body, sub=sub_id)
    return st, data


async def main():
    execute = "--execute" in sys.argv
    label = "EXECUTE" if execute else "DRY-RUN"
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"[{ts}] rebuild_quote_buffers [{label}]")
    print()

    async with aiohttp.ClientSession() as session:
        total_plan = []  # list of {sub, pair, sell_amount, est_quote, quote_needed}

        for bucket_name, bcfg in BUCKETS.items():
            cfg_path = CONFIG_DIR / bcfg["config"]
            if not cfg_path.exists():
                print(f"  SKIP {bucket_name}: no config at {cfg_path}")
                continue

            bucket_cfg = json.loads(cfg_path.read_text())
            bases = enabled_bases(bucket_cfg)
            quote = bcfg["quote"]
            target = bcfg["quote_target"]
            symbol = bcfg["quote_symbol"]

            print(f"=== {bucket_name.upper()} (target: {symbol}{target} {quote}) ===")

            for sub_name, sub_id in bcfg["subs"].items():
                bals = await get_balances(session, sub_id)
                if bals is None:
                    print(f"  {sub_name}: FAILED to get balances")
                    continue

                have_quote = bals.get(quote, 0)
                need_quote = max(0, target - have_quote)

                print(f"  {sub_name}: have {symbol}{have_quote:.4f} {quote}, need {symbol}{need_quote:.4f}")

                if need_quote < target * 0.05:  # within 5% of target, skip
                    print(f"    ✓ OK (within 5%)")
                    continue

                # Find base coins to sell
                to_sell = []
                quote_raised = 0

                for base in sorted(bases):
                    pair = f"{base}{quote}"
                    if pair in SKIP_PAIRS:
                        continue

                    have_base = bals.get(base, 0)
                    if have_base < 0.00000001:
                        continue

                    # Get price
                    price = await get_price(session, pair)
                    if not price or price <= 0:
                        print(f"    {pair}: no price")
                        continue

                    # Calculate how much to sell
                    # Keep at least 50% of base (for sell-side orders)
                    max_sell = have_base * 0.50
                    value = max_sell * price

                    if value < 0.5:
                        # Too small, might as well sell all
                        max_sell = have_base * 0.99
                        value = max_sell * price

                    if value < 0.1:
                        continue

                    # Only sell what we need
                    remaining_need = need_quote - quote_raised
                    if value >= remaining_need:
                        # Only sell what's needed
                        sell_amount = remaining_need / price
                        # Round down slightly to avoid over-selling
                        sell_amount *= 0.98
                    else:
                        sell_amount = max_sell

                    if sell_amount < 0.00000001:
                        continue

                    est_value = sell_amount * price
                    to_sell.append({
                        "sub_name": sub_name,
                        "sub_id": sub_id,
                        "pair": pair,
                        "base": base,
                        "have_base": have_base,
                        "sell_amount": sell_amount,
                        "price": price,
                        "est_quote": est_value,
                    })
                    quote_raised += est_value

                    if quote_raised >= need_quote:
                        break

                if to_sell:
                    for s in to_sell:
                        total_plan.append(s)
                        print(f"    SELL {s['sell_amount']:.8g} {s['base']} on {s['pair']} → ~{symbol}{s['est_quote']:.2f} (have {s['have_base']:.8g})")
                    total_est = sum(s["est_quote"] for s in to_sell)
                    print(f"    → Will raise ~{symbol}{total_est:.2f} {quote} (need {symbol}{need_quote:.2f})")
                else:
                    print(f"    No base to sell!")
            print()

        if not total_plan:
            print("All subs have sufficient quote buffers. Nothing to do.")
            return

        # Summary
        by_sub = {}
        for s in total_plan:
            by_sub.setdefault(s["sub_name"], []).append(s)

        print(f"=== SUMMARY ({len(total_plan)} sells across {len(by_sub)} subs) ===")
        for sub_name, sells in by_sub.items():
            bname = None
            for bn, bc in BUCKETS.items():
                if sub_name in bc["subs"]:
                    bname = bn
                    sym = bc["quote_symbol"]
                    break
            total = sum(s["est_quote"] for s in sells)
            print(f"  {sub_name} ({bname}): {len(sells)} sells → ~{sym}{total:.2f}")

        if not execute:
            print(f"\nDRY RUN. Pass --execute to execute sells.")
            return

        # Execute
        print(f"\n=== EXECUTING ===")
        ok = 0
        fail = 0
        for s in total_plan:
            print(f"  SELL {s['sell_amount']:.8g} {s['base']} on {s['pair']} (sub: {s['sub_name']})...")
            st, data = await market_sell(
                session, s["sub_id"], s["pair"], s["sell_amount"]
            )
            if st in (200, 201, 202):
                print(f"    OK: {st}")
                ok += 1
            else:
                print(f"    FAIL: {st} {data}")
                fail += 1
            await asyncio.sleep(1.5)  # rate limit

        print(f"\nResults: {ok} OK, {fail} FAIL")

        if ok > 0:
            print("\nWaiting for balances to settle...")
            await asyncio.sleep(10)
            # Show new quote balances
            for sub_name in by_sub:
                for bn, bc in BUCKETS.items():
                    if sub_name in bc["subs"]:
                        sub_id = bc["subs"][sub_name]
                        bals = await get_balances(session, sub_id)
                        if bals:
                            q = bc["quote"]
                            sym = bc["quote_symbol"]
                            print(f"  {sub_name}: {sym}{bals.get(q, 0):.4f} {q}")


if __name__ == "__main__":
    asyncio.run(main())
