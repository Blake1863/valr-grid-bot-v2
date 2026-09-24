#!/usr/bin/env python3
"""
auto_restock.py — Automated inventory restock for wash-bot CMS subs.

Detects pairs starved of base/quote inventory, buys on MAIN, transfers to subs,
and refills quote working buffers. Designed for cron execution (every 4h).

Usage:
  python3 scripts/auto_restock.py              # dry-run (default)
  python3 scripts/auto_restock.py --execute     # actually buy + transfer
  python3 scripts/auto_restock.py --budget 200  # override default budget

Budget cap: $100 default (USD-ref). Never spends more than what's available.
Safety: only buys on pairs that are genuinely starved (insufficient errors in logs).
Idempotent: skip pairs already above target.

Exit code 0 = healthy or nothing to do
Exit code 1 = action needed (dry-run with findings)
Exit code 2 = errors during execution
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.creds import load
from src.valr_rest import ValrRest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BUCKETS = ["zar", "usdt", "usdc"]

SUB_IDS = {
    "zar":   [("CMSZAR1",  1513524239074144256),
              ("CMSZAR2",  1513524288865144832)],
    "usdt":  [("CMSUSDT1", 1513524297399840768),
              ("CMSUSDT2", 1513524305939443712)],
    "usdc":  [("CMSUSDC1", 1513524314495823872),
              ("CMSUSDC2", 1513524323040333824)],
}

# Targets (per sub, USD-ref)
TARGET_BASE_PER_SUB = 17.50   # ~$35/pair total

# Quote working buffers to keep in each sub
QUOTE_BUFFER = {
    "zar": 2500,    # ZAR per sub
    "usdt": 50,     # USDT per sub
    "usdc": 3,      # USDC per sub
}

# Pairs to always skip (1-tick spread, can't wash — by design)
SKIP_PAIRS = {"SPYXUSDT", "EURCUSDC"}

# Budget cap (USD-ref equivalent)
DEFAULT_BUDGET = 100.0

# Minimum spend threshold per buy (skip micro-buys)
MIN_BUY_USD = 1.0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

def base_currency(pair: str) -> str:
    for q in ("USDT", "USDC", "ZAR"):
        if pair.endswith(q):
            return pair[:-len(q)]
    return pair

def quote_currency(pair: str) -> str:
    for q in ("USDT", "USDC", "ZAR"):
        if pair.endswith(q):
            return q
    return ""

def parse_latest_summary(bucket: str) -> dict:
    """Return {pair: {prints, ext, skip_bal, skip_spread, internal_ratio, last_err}}"""
    log_path = LOG_DIR / f"valr-cm-spot-{bucket}.log"
    if not log_path.exists():
        return {}
    lines = log_path.read_text(errors="replace").splitlines()
    # Find last summary block
    starts = [i for i, ln in enumerate(lines) if "----- summary -----" in ln]
    if not starts:
        return {}
    out = {}
    pat = re.compile(
        r"([A-Z0-9]+):\s+prints=(\d+)\s+ext=(\d+)\s+skip_bal=(\d+)"
        r"\s+skip_spread=(\d+)\s+fail=(\d+)\s+incl=(\d+)\s+internal_ratio=([0-9.]+)"
        r".*?last_err=(.*)$"
    )
    for ln in lines[starts[-1] + 1:]:
        if "-------------------" in ln:
            break
        m = pat.search(ln)
        if not m:
            continue
        pair = m.group(1)
        out[pair] = {
            "prints": int(m.group(2)),
            "ext": int(m.group(3)),
            "skip_bal": int(m.group(4)),
            "skip_spread": int(m.group(5)),
            "fail": int(m.group(6)),
            "incl": int(m.group(7)),
            "internal_ratio": float(m.group(8)),
            "last_err": m.group(9).strip(),
        }
    return out

def is_starved(stats: dict) -> bool:
    """True if pair is stopped/thin due to insufficient balance (not spread-only)."""
    le = stats.get("last_err", "").lower()
    if "skip_spread_tight" in le or "spread=" in le:
        # Pure spread skip — not an inventory issue
        return False
    if "insufficient" in le:
        return True
    # prints=0 with no insufficient but skip_bal climbing = likely starved too
    if stats.get("prints", 0) == 0 and stats.get("skip_bal", 0) > 10:
        return True
    return False

def usd_ref_price(price: float, quote: str, zar_ref: float) -> float:
    """Convert price to USD-ref (USDC)."""
    if quote == "ZAR":
        return price / zar_ref
    return price  # USDT ~= USDC, USDC = 1:1

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    execute = "--execute" in sys.argv
    budget_override = None
    for i, arg in enumerate(sys.argv):
        if arg == "--budget" and i + 1 < len(sys.argv):
            budget_override = float(sys.argv[i + 1])

    budget = budget_override if budget_override is not None else DEFAULT_BUDGET
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    label = "EXECUTE" if execute else "DRY-RUN"

    print(f"[{ts}] auto_restock [{label}] budget=${budget:.0f}")
    print()

    # Load creds
    creds = load()
    key = creds["MAIN_API_" + "KEY"]
    secret = creds["MAIN_API_" + "SECRET"]
    rest = ValrRest(key, secret)

    # MAIN balances
    st, data = await rest.request("GET", "/v1/account/balances")
    if st != 200 or not isinstance(data, list):
        print(f"ERROR: failed to get MAIN balances: {st} {data}")
        sys.exit(2)

    main_bals = {}
    for b in data:
        cur = b["currency"]
        main_bals[cur] = float(b.get("available", 0))

    # ZAR reference price
    st2, zar_data = await rest.request("GET", "/v1/public/USDCZAR/marketsummary", signed=False)
    zar_ref = 18.0  # fallback
    if st2 == 200 and isinstance(zar_data, dict):
        zar_ref = float(zar_data.get("lastTradedPrice", 18.0))

    main_usdc_ref = (
        main_bals.get("USDC", 0)
        + main_bals.get("USDT", 0)
        + main_bals.get("ZAR", 0) / zar_ref
    )
    print(f"MAIN available: USDC={main_bals.get('USDC',0):.4f}  USDT={main_bals.get('USDT',0):.4f}  ZAR={main_bals.get('ZAR',0):.2f}")
    print(f"MAIN total ≈ ${main_usdc_ref:.2f} USDC-ref (ZAR ref: {zar_ref:.2f})")
    print()

    # --- Phase 1: Detect starved pairs ---
    findings = []  # {bucket, pair, base, quote, price, need_base, cost_usd, reason}

    for bucket in BUCKETS:
        cfg_path = Path(__file__).resolve().parent.parent / f"config-{bucket}.json"
        if not cfg_path.exists():
            print(f"  SKIP {bucket}: no config")
            continue
        cfg = json.loads(cfg_path.read_text())
        pairs_cfg = cfg.get("pairs", {})
        summary = parse_latest_summary(bucket)

        for pair, pcfg in pairs_cfg.items():
            if not pcfg.get("enabled", True):
                continue
            if pair in SKIP_PAIRS:
                continue
            stats = summary.get(pair)
            if not stats:
                continue
            if not is_starved(stats):
                continue

            # Get price
            st3, pd = await rest.request("GET", f"/v1/public/{pair}/marketsummary", signed=False)
            if st3 != 200 or not isinstance(pd, dict):
                print(f"  WARN: no price for {pair}")
                continue
            price = float(pd.get("lastTradedPrice", 0) or pd.get("markPrice", 0))
            if price <= 0:
                continue

            base = base_currency(pair)
            quote = quote_currency(pair)
            have_base = main_bals.get(base, 0)
            have_quote = main_bals.get(quote, 0)

            # Target: $35/pair total (~$17.50 per sub)
            target_usd = TARGET_BASE_PER_SUB * 2
            usd_price = usd_ref_price(price, quote, zar_ref)
            target_base = target_usd / usd_price if usd_price > 0 else 0
            need_base = max(0, target_base - have_base)
            cost_quote = need_base * price
            cost_usd = need_base * usd_price

            if cost_usd < MIN_BUY_USD:
                # Already close to target or too small to bother
                continue

            reason = stats.get("last_err", "stopped")[:80]
            findings.append({
                "bucket": bucket,
                "pair": pair,
                "base": base,
                "quote": quote,
                "price": price,
                "usd_price": round(usd_price, 6),
                "have_base": round(have_base, 10),
                "need_base": round(need_base, 10),
                "cost_quote": round(cost_quote, 6),
                "cost_usd": round(cost_usd, 2),
                "reason": reason,
                "prints": stats["prints"],
            })

    if not findings:
        print("All enabled pairs healthy. No restock needed.")
        return

    # Sort by bucket then cost
    findings.sort(key=lambda f: (f["bucket"], -f["cost_usd"]))

    # --- Phase 2: Budget check & buy plan ---
    total_cost = sum(f["cost_usd"] for f in findings)

    # Also check quote buffer needs
    buffer_needs = []  # {bucket, currency, sub_name, need, cost_usd}
    for bucket in BUCKETS:
        cur_name = bucket.upper() if bucket != "usdt" else "USDT"
        # ZAR bucket uses ZAR, USDT uses USDT, USDC uses USDC
        if bucket == "zar":
            cur_name = "ZAR"
        elif bucket == "usdt":
            cur_name = "USDT"
        else:
            cur_name = "USDC"

        target_buffer = QUOTE_BUFFER[bucket]
        for sub_name, sub_id in SUB_IDS[bucket]:
            st4, sd = await rest.request("GET", "/v1/account/balances", subaccount_id=sub_id)
            if st4 != 200 or not isinstance(sd, list):
                continue
            sub_bals = {b["currency"]: float(b.get("available", 0)) for b in sd}
            have = sub_bals.get(cur_name, 0)
            need = max(0, target_buffer - have)
            if need < 0.01:
                continue
            cost_usd = need / zar_ref if cur_name == "ZAR" else need
            buffer_needs.append({
                "bucket": bucket,
                "currency": cur_name,
                "sub_name": sub_name,
                "sub_id": sub_id,
                "need": round(need, 6),
                "cost_usd": round(cost_usd, 2),
            })

    buffer_total = sum(b["cost_usd"] for b in buffer_needs)
    grand_total = total_cost + buffer_total

    print(f"=== RESTOCK PLAN ===")
    print(f"  Starved pairs: {len(findings)}")
    print(f"  Quote buffer gaps: {len(buffer_needs)}")
    print(f"  Base buy cost: ${total_cost:.2f}")
    print(f"  Buffer fill cost: ${buffer_total:.2f}")
    print(f"  Total: ${grand_total:.2f}")
    print(f"  MAIN balance: ${main_usdc_ref:.2f}")
    print(f"  Budget cap: ${budget:.2f}")
    print()

    effective_budget = min(budget, main_usdc_ref - 5.0)  # keep $5 cushion
    if effective_budget < 1:
        print(f"BUDGET: insufficient funds to restock (MAIN=${main_usdc_ref:.2f}, cushion=$5)")
        return

    # Cap to budget
    capped_findings = []
    remaining = effective_budget
    for f in findings:
        if remaining < MIN_BUY_USD:
            break
        capped_findings.append(f)
        remaining -= f["cost_usd"]

    # Trim buffer fills if over budget
    capped_buffer = []
    for b in buffer_needs:
        if remaining < 0.5:
            break
        capped_buffer.append(b)
        remaining -= b["cost_usd"]

    if len(capped_findings) < len(findings) or len(capped_buffer) < len(buffer_needs):
        print(f"BUDGET CAPPED: {len(capped_findings)}/{len(findings)} pairs, {len(capped_buffer)}/{len(buffer_needs)} buffers")
        print()

    # Print plan
    for f in capped_findings:
        bflag = "" if f["bucket"] == "usdt" else ""
        sym = "R" if f["quote"] == "ZAR" else "$"
        print(f"  BUY {f['pair']:15s} {sym}{f['cost_quote']:.2f} (~{f['need_base']:.8g} {f['base']}) — {f['reason'][:60]}")

    for b in capped_buffer:
        print(f"  BUFFER {b['sub_name']:12s} {b['need']:.2f} {b['currency']} (~${b['cost_usd']:.2f})")

    if not execute:
        print(f"\n  DRY RUN. Pass --execute to execute.")
        sys.exit(1)

    # --- Phase 3: Execute buys ---
    print(f"\n=== EXECUTING BUYS ===")
    buys_ok = 0
    buys_fail = 0
    bought_bases = set()  # track what we bought so we know what to transfer

    for f in capped_findings:
        pair = f["pair"]
        base = f["base"]
        quote = f["quote"]
        cost_quote = f["cost_quote"]
        if cost_quote < 0.01:
            continue

        # For ZAR pairs, check we have enough ZAR in MAIN
        if quote == "ZAR":
            have = main_bals.get("ZAR", 0)
            if have < cost_quote:
                print(f"  SKIP {pair}: insufficient {quote} in MAIN (need {cost_quote:.2f}, have {have:.2f})")
                buys_fail += 1
                continue

            # Check if we need to convert USDT->ZAR first
            if have < cost_quote * 1.1:  # need more ZAR
                need_zar = cost_quote * 1.05 - have
                if need_zar > 1:
                    usdtzr_st, usdtzr_pd = await rest.request("GET", "/v1/public/USDTZAR/marketsummary", signed=False)
                    usdtzr = 16.5
                    if usdtzr_st == 200 and isinstance(usdtzr_pd, dict):
                        usdtzr = float(usdtzr_pd.get("lastTradedPrice", 16.5))
                    usdt_to_sell = need_zar / usdtzr
                    if main_bals.get("USDT", 0) > usdt_to_sell + 1:
                        print(f"  Converting ${usdt_to_sell:.2f} USDT -> ~R{need_zar:.2f} ZAR...")
                        r_st, r_data = await rest.request("POST", "/v1/orders/market",
                            body={"side": "SELL", "quoteAmount": f"{usdt_to_sell:.4f}", "pair": "USDTZAR"})
                        if r_st in (200, 201, 202):
                            print(f"    OK: {r_data}")
                            await asyncio.sleep(5)  # let balance settle
                            # Refresh ZAR balance
                            r2_st, r2_data = await rest.request("GET", "/v1/account/balances")
                            if r2_st == 200 and isinstance(r2_data, list):
                                for bb in r2_data:
                                    if bb["currency"] == "ZAR":
                                        main_bals["ZAR"] = float(bb.get("available", 0))
                        else:
                            print(f"    FAIL: {r_st} {r_data}")
                            buys_fail += 1
                            continue

        # Market buy
        body = {"side": "BUY", "quoteAmount": f"{cost_quote:.4f}", "pair": pair}
        r_st, r_data = await rest.request("POST", "/v1/orders/market", body=body)
        if r_st in (200, 201, 202):
            print(f"  OK: BUY {pair} {sym if quote == 'ZAR' else '$'}{cost_quote:.2f}")
            bought_bases.add(base)
            buys_ok += 1
        else:
            print(f"  FAIL: BUY {pair}: {r_st} {r_data}")
            buys_fail += 1
        await asyncio.sleep(0.5)

    # --- Phase 4: Refresh MAIN balances ---
    print(f"\n=== REFRESHING BALANCES ===")
    await asyncio.sleep(5)
    r3_st, r3_data = await rest.request("GET", "/v1/account/balances")
    if r3_st == 200 and isinstance(r3_data, list):
        main_bals = {b["currency"]: float(b.get("available", 0)) for b in r3_data}

    # --- Phase 5: Transfers to subs ---
    print(f"\n=== TRANSFERS ===")
    transfers_ok = 0
    transfers_fail = 0

    # Base inventory transfers
    for f in capped_findings:
        base = f["base"]
        have = main_bals.get(base, 0)
        if have < 0.0000001:
            print(f"  SKIP {base}: no balance on MAIN")
            continue

        # Determine which subs to transfer to
        target_subs = SUB_IDS.get(f["bucket"], [])
        if not target_subs:
            continue

        share = have * 0.499  # leave dust in MAIN
        for sub_name, sub_id in target_subs:
            try:
                t_st, t_data = await rest.request("POST", "/v1/account/subaccounts/transfer",
                    body={
                        "fromId": 0,
                        "toId": int(sub_id),
                        "currencyCode": base,
                        "amount": f"{share:.10f}",
                        "allowBorrow": False,
                    })
                if t_st in (200, 201):
                    print(f"  OK: MAIN -> {sub_name}: {share:.8g} {base}")
                    transfers_ok += 1
                else:
                    print(f"  FAIL: MAIN -> {sub_name} {base}: {t_st} {t_data}")
                    transfers_fail += 1
            except Exception as e:
                print(f"  FAIL: MAIN -> {sub_name} {base}: {e}")
                transfers_fail += 1
            await asyncio.sleep(0.2)

    # Quote buffer transfers
    for b in capped_buffer:
        cur = b["currency"]
        have = main_bals.get(cur, 0)
        if have < b["need"] * 0.5:
            print(f"  SKIP {cur} buffer: insufficient MAIN balance ({have:.4f})")
            continue

        amount = min(b["need"], have * 0.49)
        if amount < 0.01:
            continue

        try:
            t_st, t_data = await rest.request("POST", "/v1/account/subaccounts/transfer",
                body={
                    "fromId": 0,
                    "toId": int(b["sub_id"]),
                    "currencyCode": cur,
                    "amount": f"{amount:.10f}",
                    "allowBorrow": False,
                })
            if t_st in (200, 201):
                print(f"  OK: MAIN -> {b['sub_name']}: {amount:.4f} {cur} (buffer)")
                transfers_ok += 1
            else:
                print(f"  FAIL: MAIN -> {b['sub_name']} {cur}: {t_st} {t_data}")
                transfers_fail += 1
        except Exception as e:
            print(f"  FAIL: MAIN -> {b['sub_name']} {cur}: {e}")
            transfers_fail += 1
        await asyncio.sleep(0.2)

    # --- Summary ---
    print(f"\n=== SUMMARY ===")
    print(f"  Buys: {buys_ok} OK, {buys_fail} FAIL")
    print(f"  Transfers: {transfers_ok} OK, {transfers_fail} FAIL")
    if buys_fail > 0 or transfers_fail > 0:
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
