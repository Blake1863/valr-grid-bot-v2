#!/usr/bin/env python3
"""
Daily Reconciliation
====================
Formula (per day, all 4 subaccounts combined):

  fees_estimated     = actual_perp_fees + spot_taker_notional * 0.0002
  inventory_movement = closing - opening + fees_estimated

Volume is sourced from /v1/account/transactionhistory (supports date filters,
much faster than paginating /v1/account/tradehistory which lacks date support).

Outputs:
  - Telegram message with daily summary
  - Appends to scripts/recon_log.json (one entry per date)

Cron (22:05 UTC = 00:05 SAST next day):
  5 22 * * * /usr/bin/python3 /home/admin/.openclaw/workspace/bots/cm-bot-v2/scripts/daily_recon.py \
             >> /home/admin/.openclaw/workspace/bots/cm-bot-v2/logs/daily-recon.log 2>&1

Usage:
  python3 daily_recon.py              # yesterday (SAST)
  python3 daily_recon.py 2026-04-01   # specific date
  python3 daily_recon.py --dry-run    # print only, no Telegram/log write
  python3 daily_recon.py monthly      # monthly summary for current month
  python3 daily_recon.py monthly 2026-04  # monthly summary for specific month
"""

import hashlib
import hmac
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL   = "https://api.valr.com"
SAST       = timezone(timedelta(hours=2))
FEE_RATE   = 0.0002  # 2bps taker

STATE_FILE = Path(__file__).parent / "recon_state.json"
LOG_FILE   = Path(__file__).parent / "recon_log.json"

TELEGRAM_CHAT_ID = "7018990694"

SUBACCOUNTS = {
    "CM1":  "1483472097578319872",
    "CM2":  "1483472079069155328",
    "CMS1": "1483815480334401536",
    "CMS2": "1483815498551132160",
}

# Additional accounts included in global balance but not in volume tracking
ALL_ACCOUNTS = {
    **SUBACCOUNTS,
    "Main":     "",                    # primary account (no sub-account header)
    "GridBot":  "1432690254033137664", # SOLUSDTPERP grid bot
}

# NOTE: Alibaba Secrets Manager keys are stale — using same keys as other cm-bot scripts.
# TODO: Update secrets manager with current VALR API keys.
MAIN_API_KEY    = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
MAIN_API_SECRET = "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"

# Transaction types to fetch
SPOT_TX_TYPES    = {"LIMIT_BUY", "LIMIT_SELL", "MARKET_BUY", "MARKET_SELL"}
FUTURES_FEE_TYPE = "FUTURES_TRADE_FEE"
ALL_TX_TYPES     = ",".join(SPOT_TX_TYPES | {FUTURES_FEE_TYPE})

# ── VALR Auth ─────────────────────────────────────────────────────────────────

def _headers(verb: str, path: str, body: str = "", sub_id: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{verb}{path}{body}{sub_id}"
    sig = hmac.new(MAIN_API_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()
    return {
        "X-VALR-API-KEY":        MAIN_API_KEY,
        "X-VALR-SIGNATURE":      sig,
        "X-VALR-TIMESTAMP":      ts,
        "X-VALR-SUB-ACCOUNT-ID": sub_id,
        "Content-Type":          "application/json",
    }


def _get(path: str, sub_id: str = "") -> list | dict:
    url = BASE_URL + path
    r = requests.get(url, headers=_headers("GET", path, "", sub_id), timeout=20)
    r.raise_for_status()
    return r.json()

# ── Balance ───────────────────────────────────────────────────────────────────

def get_balance(sub_id: str) -> float:
    """Total USDT-reference balance for a subaccount.
    Uses totalInReference for every asset (covers non-USDT holdings on CMS accounts).
    """
    balances = _get("/v1/account/balances", sub_id)
    return sum(float(b.get("totalInReference") or 0) for b in balances)

# ── Volume via transactionhistory ─────────────────────────────────────────────

def fetch_txn_volume(sub_id: str, label: str, start_iso: str, end_iso: str) -> dict:
    """
    Fetches LIMIT_BUY/SELL + FUTURES_TRADE_FEE transactions for the date window.
    Returns:
      spot_volume    - one-sided USDT notional (each trade counted once)
      perp_fees_usdt - sum of actual FUTURES_TRADE_FEE feeValues
      perp_taker_vol - back-calculated: perp_fees / FEE_RATE
      fill_count     - spot fill count
    """
    spot_volume    = 0.0
    perp_fees_usdt = 0.0
    fill_count     = 0
    before_id      = None

    while True:
        path = (
            f"/v1/account/transactionhistory"
            f"?limit=200"
            f"&transactionTypes={ALL_TX_TYPES}"
            f"&startTime={start_iso}"
            f"&endTime={end_iso}"
        )
        if before_id:
            path += f"&beforeId={before_id}"

        try:
            batch = _get(path, sub_id)
        except Exception as e:
            print(f"  [WARN] Txn history failed for {label}: {e}")
            break

        if not batch or not isinstance(batch, list):
            break

        for tx in batch:
            tx_type   = tx.get("transactionType", {}).get("type", "")
            debit_ccy = tx.get("debitCurrency", "")
            credit_ccy = tx.get("creditCurrency", "")
            debit_val  = float(tx.get("debitValue") or 0)
            credit_val = float(tx.get("creditValue") or 0)
            fee_val    = float(tx.get("feeValue") or 0)

            if tx_type in ("LIMIT_BUY", "MARKET_BUY"):
                if debit_ccy in ("USDT", "USDC"):
                    spot_volume += debit_val
                fill_count += 1

            elif tx_type in ("LIMIT_SELL", "MARKET_SELL"):
                if credit_ccy in ("USDT", "USDC"):
                    spot_volume += credit_val
                fill_count += 1

            elif tx_type == FUTURES_FEE_TYPE:
                perp_fees_usdt += fee_val

        if len(batch) < 200:
            break

        before_id = batch[-1]["id"]

    # Perp taker notional back-calculated from actual fees (0% maker, 2bps taker)
    perp_taker_vol = perp_fees_usdt / FEE_RATE if FEE_RATE > 0 else 0.0

    print(f"  {label}: spot=${spot_volume:,.2f}  perp_fees=${perp_fees_usdt:.4f}  fills={fill_count}")
    return {
        "spot_volume":    round(spot_volume,    4),
        "perp_taker_vol": round(perp_taker_vol, 4),
        "perp_fees_usdt": round(perp_fees_usdt, 6),
        "fill_count":     fill_count,
    }

# ── State / Log ───────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_log() -> list:
    if LOG_FILE.exists():
        return json.loads(LOG_FILE.read_text())
    return []


def save_log(entries: list):
    LOG_FILE.write_text(json.dumps(entries, indent=2))

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    import subprocess
    result = subprocess.run(
        ["openclaw", "message", "send",
         "--channel", "telegram",
         "--target", TELEGRAM_CHAT_ID,
         "--message", message],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"[WARN] Telegram error: {result.stderr}")

# ── Monthly Summary ───────────────────────────────────────────────────────────

def build_monthly_summary(log: list, year_month: str) -> str:
    days = [d for d in log if d["date"].startswith(year_month)]
    if not days:
        return f"No data for {year_month}"

    days.sort(key=lambda d: d["date"])
    spot_vol     = sum(d.get("spot_volume",   0) for d in days)
    perp_taker   = sum(d.get("perp_taker_vol", 0) for d in days)
    fees_est     = sum(d["fees_estimated"]       for d in days)
    inv_movement = sum(d["inventory_movement"]   for d in days)
    fill_count   = sum(d.get("fill_count", 0)    for d in days)
    opening      = days[0]["opening"]
    closing      = days[-1]["closing"]
    inv_emoji    = "🟢" if inv_movement >= 0 else "🔴"

    return (
        f"📊 *Monthly Summary — {year_month}*\n"
        f"\n"
        f"Opening: `${opening:,.2f}`\n"
        f"Closing: `${closing:,.2f}`\n"
        f"\n"
        f"Spot vol:  `${spot_vol:,.2f}`  _{fill_count} fills_\n"
        f"Perp vol:  `${perp_taker:,.2f}` taker\n"
        f"Fees est:  `${fees_est:,.4f}`\n"
        f"\n"
        f"{inv_emoji} *Inventory movement:* `${inv_movement:+,.4f}`\n"
        f"Days tracked: `{len(days)}`"
    )

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    dry_run  = "--dry-run" in sys.argv
    args     = [a for a in sys.argv[1:] if not a.startswith("--")]
    now_sast = datetime.now(SAST)

    # Monthly summary mode
    if args and args[0] == "monthly":
        ym = args[1] if len(args) > 1 else now_sast.strftime("%Y-%m")
        print(build_monthly_summary(load_log(), ym))
        return

    # Date arg or yesterday
    if args:
        target_date = datetime.strptime(args[0], "%Y-%m-%d").replace(tzinfo=SAST)
    else:
        target_date = (now_sast - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)

    date_str = target_date.strftime("%Y-%m-%d")

    # UTC window for the SAST day
    start_sast = target_date.replace(hour=0,  minute=0,  second=0,  microsecond=0)
    end_sast   = target_date.replace(hour=23, minute=59, second=59, microsecond=999000)
    start_iso  = start_sast.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso    = end_sast.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.999Z")

    print(f"[{now_sast.strftime('%Y-%m-%d %H:%M SAST')}] Daily recon for {date_str}")
    print(f"UTC window: {start_iso} → {end_iso}")
    if dry_run:
        print("[DRY RUN] — no Telegram, no log write")

    # ── Closing balances ──────────────────────────────────────────────────────
    print("\nFetching closing balances...")
    closing_by_acct = {}
    for label, sub_id in ALL_ACCOUNTS.items():
        bal = get_balance(sub_id)
        closing_by_acct[label] = bal
        print(f"  {label}: ${bal:,.4f}")
    total_closing  = sum(closing_by_acct[l] for l in SUBACCOUNTS)  # CM/CMS only (recon basis)
    global_closing = sum(closing_by_acct.values())                 # all accounts
    print(f"  CM/CMS: ${total_closing:,.4f}  |  Global: ${global_closing:,.4f}")

    # ── Opening balance (from state) ──────────────────────────────────────────
    state       = load_state()
    prev_date   = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
    opening_key = f"closing_{prev_date}"
    acct_opening_key = f"closing_by_acct_{prev_date}"

    if opening_key in state:
        total_opening    = state[opening_key]
        global_opening   = state.get(f"global_closing_{prev_date}", total_opening)
        opening_by_acct  = state.get(acct_opening_key, {})
        print(f"\nOpening ({prev_date}): CM/CMS=${total_opening:,.4f}  Global=${global_opening:,.4f}")
    else:
        print(f"\n[WARN] No opening balance for {prev_date} — bootstrapping with current closing.")
        total_opening   = total_closing
        global_opening  = global_closing
        opening_by_acct = {lbl: round(v, 4) for lbl, v in closing_by_acct.items()}

    # ── Volume via transactionhistory ─────────────────────────────────────────
    print("\nFetching volume via transaction history (parallel)...")
    vol_by_acct    = {}
    all_spot_vol   = 0.0
    all_perp_fees  = 0.0
    all_perp_taker = 0.0
    all_fills      = 0

    def _fetch(args):
        label, sub_id = args
        return label, fetch_txn_volume(sub_id, label, start_iso, end_iso)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch, item): item[0] for item in SUBACCOUNTS.items()}
        for future in as_completed(futures):
            label, v = future.result()
            vol_by_acct[label]  = v
            all_spot_vol       += v["spot_volume"]
            all_perp_fees      += v["perp_fees_usdt"]
            all_perp_taker     += v["perp_taker_vol"]
            all_fills          += v["fill_count"]

    # Spot: each buy/sell tx is one side. In wash cycle CMS1 buys + CMS2 sells
    # → both appear, so spot_volume already double-counts the notional.
    # Taker spot ≈ spot_volume / 2 (one side per cycle).
    taker_spot_est = all_spot_vol / 2
    spot_fees_est  = taker_spot_est * FEE_RATE
    fees_estimated = spot_fees_est + all_perp_fees

    inv_movement = total_closing - total_opening + fees_estimated
    inv_emoji    = "🟢" if inv_movement >= 0 else "🔴"

    print(f"\nOpening:            ${total_opening:,.4f}")
    print(f"Closing:            ${total_closing:,.4f}")
    print(f"Spot volume (2-sd): ${all_spot_vol:,.2f}  → taker est ${taker_spot_est:,.2f}")
    print(f"Perp taker vol:     ${all_perp_taker:,.2f}  (fees÷2bps)")
    print(f"Fees estimated:     ${fees_estimated:,.4f}  (spot est + actual perp)")
    print(f"Inventory movement: ${inv_movement:+,.4f}")

    # ── Log entry ─────────────────────────────────────────────────────────────
    log_entry = {
        "date":               date_str,
        "opening":            round(total_opening,   4),
        "closing":            round(total_closing,   4),
        "global_opening":     round(global_opening,  4),
        "global_closing":     round(global_closing,  4),
        "spot_volume":        round(all_spot_vol,    4),
        "perp_taker_vol":     round(all_perp_taker,  4),
        "perp_fees_usdt":     round(all_perp_fees,   6),
        "fees_estimated":     round(fees_estimated,  6),
        "inventory_movement": round(inv_movement,    6),
        "fill_count":         all_fills,
        "accounts": {
            lbl: {
                **v,
                "opening": opening_by_acct.get(lbl, None),
                "closing": round(closing_by_acct[lbl], 4),
            }
            for lbl, v in vol_by_acct.items()
        },
    }

    # ── Telegram message ──────────────────────────────────────────────────────
    net_change        = total_closing - total_opening
    global_net_change = global_closing - global_opening
    net_emoji         = "🟢" if net_change >= 0 else "🔴"
    global_emoji      = "🟢" if global_net_change >= 0 else "🔴"
    total_vol         = all_spot_vol / 2 + all_perp_taker  # single-sided notional

    msg  = f"📊 *Daily Recon — {date_str}*\n"
    msg += f"\n"
    msg += f"*Global (all accounts)*\n"
    msg += f"🟦 Opening:  `${global_opening:,.2f}`\n"
    msg += f"🟦 Closing:  `${global_closing:,.2f}`\n"
    msg += f"{global_emoji} Net change: `${global_net_change:+,.2f}`\n"
    msg += f"\n"
    msg += f"*CM/CMS bots*\n"
    msg += f"🟦 Opening:  `${total_opening:,.2f}`\n"
    msg += f"🟦 Closing:  `${total_closing:,.2f}`\n"
    msg += f"{net_emoji} Net change: `${net_change:+,.2f}`\n"
    msg += f"\n"
    msg += f"📈 *Volume*\n"
    msg += f"  Spot:  `${all_spot_vol / 2:,.2f}` taker  _{all_fills} fills_\n"
    msg += f"  Perp:  `${all_perp_taker:,.2f}` taker  _(actual fees ÷ 2bps)_\n"
    msg += f"  Total: `${total_vol:,.2f}` notional\n"
    msg += f"\n"
    msg += f"💸 *Fees (estimated)*: `${fees_estimated:,.2f}`\n"
    msg += f"\n"
    msg += f"{inv_emoji} *Inventory movement*: `${inv_movement:+,.2f}`\n"
    msg += f"_= closing − opening + fees_\n"
    msg += f"\n"
    msg += f"――――――――――――――――――――\n"
    msg += f"_Account detail_\n"
    msg += f"```\n"
    msg += f"{'Acct':<8} {'Open':>8} {'Close':>8}\n"
    msg += f"{'─'*8} {'─'*8} {'─'*8}\n"
    for lbl in ALL_ACCOUNTS:
        op  = opening_by_acct.get(lbl)
        cl  = closing_by_acct.get(lbl, 0.0)
        op_str = f"${op:,.2f}" if op is not None else "   n/a"
        msg += f"{lbl:<8} {op_str:>8} ${cl:>7,.2f}\n"
    msg += f"```"

    print("\n--- Telegram ---")
    print(msg)
    print("---")

    if not dry_run:
        log = load_log()
        log = [d for d in log if d["date"] != date_str]
        log.append(log_entry)
        log.sort(key=lambda d: d["date"])
        save_log(log)
        print(f"\nLog saved ({len(log)} entries)")

        state[f"closing_{date_str}"]            = round(total_closing, 4)
        state[f"global_closing_{date_str}"]     = round(global_closing, 4)
        state[f"closing_by_acct_{date_str}"]    = {lbl: round(v, 4) for lbl, v in closing_by_acct.items()}
        save_state(state)
        print(f"State: closing_{date_str} = ${total_closing:,.2f}")
        for lbl, v in closing_by_acct.items():
            print(f"  {lbl}: ${v:,.4f}")

        send_telegram(msg)
        print("Telegram sent.")
    else:
        print("\n[DRY RUN] Skipped log/state write and Telegram.")


if __name__ == "__main__":
    main()
