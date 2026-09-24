#!/usr/bin/env python3
"""
inventory_restock_check.py — Level 1 restock alerter (detect + propose, no spend).

Reads the latest "----- summary -----" block from each wash-bot bucket log,
flags pairs that are starved of base inventory (stopped or thin), pulls live
public prices, and prints a ready-to-execute buy list (bought from the main
account, then transferred into that bucket's two subaccounts).

NOTHING is purchased or transferred. Output is for human approval.

Detection rules (per enabled pair, from latest summary):
  - STOPPED : prints == 0 AND skip_bal > 0 AND last_err contains "insufficient"
  - THIN    : prints > 0 AND skip_bal >= SKIP_BAL_THIN  (degraded but alive)
  - Pairs whose last_err is purely SKIP_SPREAD_TIGHT are IGNORED
    (cannot wash at <2-tick spread — by design, not an inventory problem).

Sizing:
  TARGET_USD_PER_SUB base float per sub x 2 subs = base USD per pair.
  Quote side (USDT/ZAR) is rarely the blocker, so only base is proposed.

Exit codes: 0 always (alerting tool). Use --json for machine output.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BOT_DIR / "logs"
VALR_REQ = Path("/home/admin/.openclaw/workspace/skills/valr-exchange/scripts/valr_request.py")

BUCKETS = ("zar", "usdt", "usdc")

# --- tunables ---
TARGET_USD_PER_SUB = 17.5           # base float to seed per sub ($35/pair, Blake 2026-06-18)
SUBS = 2
SKIP_BAL_THIN = 60                 # skip_bal in latest window => "thin" alert
# pairs to never alert on (handled by design / out of scope)
IGNORE_PAIRS = {"SPYXUSDT", "EURCUSDC"}  # permanent 1-tick spread

SUMMARY_LINE_RE = re.compile(
    r"([A-Z0-9]+(?:USDT|USDC|ZAR)):\s+prints=(\d+)\s+ext=(\d+)\s+skip_bal=(\d+)"
    r"\s+skip_spread=(\d+)\s+fail=(\d+)\s+incl=(\d+)\s+internal_ratio=([0-9.]+)"
    r".*?last_err=(.*)$"
)


def load_config(bucket: str) -> dict:
    return json.loads((BOT_DIR / f"config-{bucket}.json").read_text())


def enabled_pairs(cfg: dict) -> set[str]:
    return {k for k, v in cfg["pairs"].items() if v.get("enabled", True)}


def _parse_block(lines: list[str], start: int) -> dict[str, dict]:
    block = []
    for ln in lines[start + 1:]:
        if "-------------------" in ln:
            break
        block.append(ln)
    out: dict[str, dict] = {}
    for ln in block:
        m = SUMMARY_LINE_RE.search(ln)
        if not m:
            continue
        pair, prints, ext, skip_bal, skip_spread, fail, incl, ratio, last_err = m.groups()
        out[pair] = {
            "prints": int(prints), "ext": int(ext), "skip_bal": int(skip_bal),
            "skip_spread": int(skip_spread), "fail": int(fail), "incl": int(incl),
            "internal_ratio": float(ratio), "last_err": last_err.strip(),
        }
    return out


def latest_two_summaries(bucket: str) -> tuple[dict, dict]:
    """Return (latest, previous) summary blocks for delta-based detection."""
    log = LOG_DIR / f"valr-cm-spot-{bucket}.log"
    if not log.exists():
        return {}, {}
    lines = log.read_text(errors="replace").splitlines()
    starts = [i for i, ln in enumerate(lines) if "----- summary -----" in ln]
    if not starts:
        return {}, {}
    latest = _parse_block(lines, starts[-1])
    prev = _parse_block(lines, starts[-2]) if len(starts) >= 2 else {}
    return latest, prev


def latest_summary(bucket: str) -> dict[str, dict]:
    """Parse the most recent summary block for a bucket. Returns pair -> stats."""
    log = LOG_DIR / f"valr-cm-spot-{bucket}.log"
    if not log.exists():
        return {}
    lines = log.read_text(errors="replace").splitlines()
    # find last summary block boundaries
    starts = [i for i, ln in enumerate(lines) if "----- summary -----" in ln]
    if not starts:
        return {}
    start = starts[-1]
    block = []
    for ln in lines[start + 1:]:
        if "-------------------" in ln:
            break
        block.append(ln)
    out: dict[str, dict] = {}
    for ln in block:
        m = SUMMARY_LINE_RE.search(ln)
        if not m:
            continue
        pair, prints, ext, skip_bal, skip_spread, fail, incl, ratio, last_err = m.groups()
        out[pair] = {
            "prints": int(prints), "ext": int(ext), "skip_bal": int(skip_bal),
            "skip_spread": int(skip_spread), "fail": int(fail), "incl": int(incl),
            "internal_ratio": float(ratio), "last_err": last_err.strip(),
        }
    return out


def classify(stats: dict, prev: dict | None = None) -> str | None:
    """Classify a pair's health.

    STOPPED: not printing at all + insufficient-balance error.
    THIN:    still printing but the CURRENT error is an insufficient-balance error
             (i.e. actively bumping the floor) AND it's barely printing this window.

    NOTE: skip_bal is a LIFETIME-in-window counter that never resets, so it is NOT
    a reliable THIN signal on its own (caused false alerts on 2026-06-18). We now
    use delta-prints (is it printing THIS window?) + current insufficient error.
    """
    le = stats["last_err"].lower()
    spread_only = "skip_spread_tight" in le or "spread=" in le
    insufficient = "insufficient" in le
    if stats["prints"] == 0:
        if insufficient or (stats["skip_bal"] > 0 and not spread_only):
            return "STOPPED"
        return None  # 0 prints purely from spread gate -> ignore
    # THIN: only if the pair is CURRENTLY hitting an insufficient-balance wall.
    # Delta prints since previous window tells us if it's actually stalling.
    if insufficient:
        if prev is not None and stats["pair"] in prev:
            delta = stats["prints"] - prev[stats["pair"]]["prints"]
            # printing healthily despite a stale insufficient last_err -> ignore
            if delta >= 5:
                return None
        return "THIN"
    return None


def base_currency(pair: str) -> str:
    for q in ("USDT", "USDC", "ZAR"):
        if pair.endswith(q):
            return pair[: -len(q)]
    return pair


def quote_currency(pair: str) -> str:
    for q in ("USDT", "USDC", "ZAR"):
        if pair.endswith(q):
            return q
    return ""


def batch_public_prices(pairs: list[str]) -> dict[str, float]:
    """Fetch last traded prices via parallel curl — one subprocess per pair, no python overhead."""
    import concurrent.futures

    def _fetch(pair: str) -> tuple[str, float | None]:
        try:
            r = subprocess.run(
                ["curl", "-sf", "-m", "8",
                 f"https://api.valr.com/v1/public/{pair}/marketsummary"],
                capture_output=True, text=True, timeout=15,
            )
            d = json.loads(r.stdout)
            p = d.get("lastTradedPrice") or d.get("markPrice")
            return pair, float(p) if p else None
        except Exception:
            return pair, None

    results: dict[str, float] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for pair, price in pool.map(_fetch, pairs):
            if price is not None:
                results[pair] = price
    return results


def zar_per_usd(prices: dict[str, float]) -> float:
    p = prices.get("USDCZAR")
    return p if p else 18.0  # fallback


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    # Phase 1: classify all pairs, collect which ones need prices
    to_fetch: list[str] = []
    flagged: list[tuple[str, str, dict]] = []  # (pair, bucket, stats_with_kind)
    for bucket in BUCKETS:
        cfg = load_config(bucket)
        enabled = enabled_pairs(cfg)
        summ, prev = latest_two_summaries(bucket)
        for pair in sorted(enabled):
            if pair in IGNORE_PAIRS:
                continue
            stats = summ.get(pair)
            if not stats:
                continue
            stats["pair"] = pair  # must be set before classify() which uses it
            kind = classify(stats, prev)
            if not kind:
                continue
            to_fetch.append(pair)
            flagged.append((pair, bucket, {**stats, "kind": kind}))

    # Phase 2: fetch all needed prices in one parallel batch
    prices = batch_public_prices(to_fetch)
    zarusd = zar_per_usd(prices)
    target_per_pair = TARGET_USD_PER_SUB * SUBS
    findings = []

    for pair, bucket, stats in flagged:
        cfg = load_config(bucket)
        g = cfg["global"]
        sub_a = (g.get("account_a_name"), g.get("account_a_id"))
        sub_b = (g.get("account_b_name"), g.get("account_b_id"))
        price = prices.get(pair)
        if not price:
            findings.append({"bucket": bucket, "pair": pair, "kind": stats["kind"],
                             "error": "no price", **{k: v for k, v in stats.items() if k != "pair"}})
            continue
        usd_price = price / zarusd if quote_currency(pair) == "ZAR" else price
        base_qty = target_per_pair / usd_price
        findings.append({
            "bucket": bucket, "pair": pair, "kind": stats["kind"],
            "base": base_currency(pair), "quote": quote_currency(pair),
            "price": price, "usd_price": round(usd_price, 6),
            "buy_base_qty": base_qty, "usd_cost": round(target_per_pair, 2),
            "subs": [sub_a, sub_b],
            "prints": stats["prints"], "skip_bal": stats["skip_bal"],
            "last_err": stats["last_err"],
        })

    if args.json:
        print(json.dumps({"ts": int(time.time()), "zar_per_usd": zarusd,
                          "findings": findings}, indent=1))
        return 0

    # human / telegram-friendly text
    stopped = [f for f in findings if f["kind"] == "STOPPED"]
    thin = [f for f in findings if f["kind"] == "THIN"]
    if not findings:
        print("OK: all enabled wash pairs healthy. No restock needed.")
        return 0

    total = sum(f.get("usd_cost", 0) for f in findings)
    lines = []
    lines.append("⚠️ Wash-bot inventory restock needed (Level 1 — review & approve)")
    lines.append(f"ZAR/USD ref: {zarusd:.2f} | est total ≈ ${total:.0f} (buy from MAIN acct)")
    lines.append("")

    def fmt(group, title):
        if not group:
            return
        lines.append(f"{title}:")
        for f in group:
            if "buy_base_qty" not in f:
                lines.append(f"  • {f['pair']}: (price fetch failed) — {f.get('last_err','')[:50]}")
                continue
            qty = f["buy_base_qty"]
            qstr = f"{qty:.8g}"
            lines.append(
                f"  • {f['pair']} [{f['bucket']}]: buy ~{qstr} {f['base']} "
                f"(≈${f['usd_cost']:.0f}) — prints={f['prints']} skip_bal={f['skip_bal']}"
            )
        lines.append("")

    fmt(stopped, "🔴 STOPPED (0 prints)")
    fmt(thin, "🟠 THIN (degraded)")
    lines.append("Reply to approve and I'll market-buy on main + transfer to the subs.")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
