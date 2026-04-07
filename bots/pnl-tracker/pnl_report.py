#!/usr/bin/env python3
"""
VALR Daily PnL Reporter
-----------------------
Fetches realised PnL + funding for the grid bot account (main account)
via /v1/account/transactionhistory, appends to ledger,
sends Telegram summary at 08:00 GMT+8.

Cron (runs at 00:00 UTC = 08:00 GMT+8):
  0 0 * * * /usr/bin/python3 /home/admin/.openclaw/workspace/bots/pnl-tracker/pnl_report.py \
            >> /home/admin/.openclaw/workspace/bots/pnl-tracker/cron.log 2>&1

PnL sources (via transactionhistory):
  FUTURES_PNL_PROFIT  → creditValue  (positive PnL)
  FUTURES_PNL_LOSS    → debitValue   (negative PnL)
  FUTURES_TRADE_FEE   → debitValue - creditValue (net fee cost)
  FUTURES_FUNDING_EARNED → creditValue
  FUTURES_FUNDING_PAID   → debitValue
"""

import hashlib
import hmac
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL  = "https://api.valr.com"
API_KEY   = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
API_SECRET= "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"

PAIRS          = ["SOLUSDTPERP"]
GRID_BOT_SUB_ID = "1432690254033137664"  # Grid Bot 1 subaccount
LEDGER_FILE    = Path(__file__).parent / "pnl_ledger.json"
TELEGRAM_CHAT_ID = "7018990694"
GMT8           = timezone(timedelta(hours=8))

TX_TYPES = [
    "FUTURES_PNL_PROFIT",
    "FUTURES_PNL_LOSS",
    "FUTURES_TRADE_FEE",
    "FUTURES_FUNDING_EARNED",
    "FUTURES_FUNDING_PAID",
]

# ── Auth ──────────────────────────────────────────────────────────────────────

def _headers(verb: str, path: str, body: str = "", sub_id: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{verb}{path}{body}{sub_id}"
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()
    return {
        "X-VALR-API-KEY":        API_KEY,
        "X-VALR-SIGNATURE":      sig,
        "X-VALR-TIMESTAMP":      ts,
        "X-VALR-SUB-ACCOUNT-ID": sub_id,
    }


def _get(path: str, sub_id: str = "") -> list | dict:
    r = requests.get(BASE_URL + path, headers=_headers("GET", path, "", sub_id), timeout=20)
    r.raise_for_status()
    return r.json()

# ── Transaction Fetching ──────────────────────────────────────────────────────

def fetch_transactions(start_iso: str, end_iso: str, sub_id: str = "") -> list:
    """Fetch all relevant transactions for the given UTC date range, paginated."""
    types_param = ",".join(TX_TYPES)
    all_txns = []
    before_id = None

    while True:
        path = (
            f"/v1/account/transactionhistory"
            f"?limit=200"
            f"&transactionTypes={types_param}"
            f"&startTime={start_iso}"
            f"&endTime={end_iso}"
        )
        if before_id:
            path += f"&beforeId={before_id}"

        batch = _get(path, sub_id)
        if not batch:
            break

        all_txns.extend(batch)

        if len(batch) < 200:
            break

        before_id = batch[-1]["id"]
        time.sleep(0.3)

    return all_txns


def summarise(txns: list, pair: str) -> dict:
    """Extract PnL, fees, funding for a specific pair from transaction list."""
    realised_pnl  = 0.0
    trading_fees  = 0.0
    funding       = 0.0
    positions_closed = 0
    funding_rates = []

    for tx in txns:
        tx_type = tx.get("transactionType", {}).get("type", "")
        additional = tx.get("additionalInfo", {}) or {}
        tx_pair = additional.get("currencyPair", "")

        # Filter by pair if additionalInfo has it; otherwise include all
        if tx_pair and tx_pair != pair:
            continue

        credit = float(tx.get("creditValue") or 0)
        debit  = float(tx.get("debitValue") or 0)

        if tx_type == "FUTURES_PNL_PROFIT":
            realised_pnl += credit
            positions_closed += 1
        elif tx_type == "FUTURES_PNL_LOSS":
            realised_pnl -= debit
            positions_closed += 1
        elif tx_type == "FUTURES_TRADE_FEE":
            trading_fees += debit - credit  # net cost (positive = cost paid)
        elif tx_type == "FUTURES_FUNDING_EARNED":
            funding += credit
            if additional.get("fundingRate"):
                funding_rates.append(float(additional["fundingRate"]))
        elif tx_type == "FUTURES_FUNDING_PAID":
            funding -= debit
            if additional.get("fundingRate"):
                funding_rates.append(-float(additional["fundingRate"]))

    avg_funding_rate = sum(funding_rates) / len(funding_rates) if funding_rates else 0.0
    net_pnl = realised_pnl + funding  # fees already deducted from realised_pnl by VALR

    return {
        "pair":             pair,
        "realised_pnl":     realised_pnl,
        "trading_fees":     trading_fees,
        "funding":          funding,
        "avg_funding_rate": avg_funding_rate,
        "positions_closed": positions_closed,
        "net_pnl":          net_pnl,
    }

# ── Balance ───────────────────────────────────────────────────────────────────

def get_usdt_balance(sub_id: str = "") -> float:
    """Sum all asset values in USDC reference terms (covers futures margin collateral)."""
    balances = _get("/v1/account/balances", sub_id)
    total = 0.0
    for b in balances:
        if b.get("totalInReference"):
            total += float(b["totalInReference"])
    return total

# ── Ledger ────────────────────────────────────────────────────────────────────

def load_ledger() -> dict:
    if LEDGER_FILE.exists():
        return json.loads(LEDGER_FILE.read_text())
    return {"inception_balance": None, "days": []}


def save_ledger(ledger: dict):
    LEDGER_FILE.write_text(json.dumps(ledger, indent=2))

# ── Report Builder ────────────────────────────────────────────────────────────

def build_report(date_str: str, summaries: list, usdt_balance: float, ledger: dict):
    if ledger["inception_balance"] is None:
        ledger["inception_balance"] = usdt_balance

    total_net      = sum(s["net_pnl"]          for s in summaries)
    total_rpnl     = sum(s["realised_pnl"]      for s in summaries)
    total_fees     = sum(s["trading_fees"]       for s in summaries)
    total_funding  = sum(s["funding"]            for s in summaries)
    total_positions= sum(s["positions_closed"]   for s in summaries)

    day_record = {
        "date":             date_str,
        "net_pnl":          round(total_net, 4),
        "realised_pnl":     round(total_rpnl, 4),
        "trading_fees":     round(total_fees, 4),
        "funding":          round(total_funding, 4),
        "positions_closed": total_positions,
        "closing_balance":  round(usdt_balance, 4),
        "pairs":            {s["pair"]: s for s in summaries},
    }

    ledger["days"] = [d for d in ledger["days"] if d["date"] != date_str]
    ledger["days"].append(day_record)
    ledger["days"].sort(key=lambda d: d["date"])

    all_days  = ledger["days"]
    cum_net   = sum(d["net_pnl"] for d in all_days)
    wins      = sum(1 for d in all_days if d["net_pnl"] > 0)
    losses    = sum(1 for d in all_days if d["net_pnl"] < 0)
    week      = sorted(all_days, key=lambda d: d["date"])[-7:]
    week_pnl  = sum(d["net_pnl"] for d in week)
    best      = max(all_days, key=lambda d: d["net_pnl"])
    worst     = min(all_days, key=lambda d: d["net_pnl"])

    emoji      = "🟢" if total_net  >= 0 else "🔴"
    week_emoji = "📈" if week_pnl   >= 0 else "📉"
    fund_emoji = "💰" if total_funding >= 0 else "💸"

    capital     = ledger["inception_balance"] or usdt_balance
    days_tracked= len(all_days)
    avg_daily   = cum_net / days_tracked if days_tracked else 0.0
    monthly_pnl = avg_daily * 30
    annual_pnl  = avg_daily * 365
    monthly_roi = (monthly_pnl / capital * 100) if capital else 0.0
    annual_roi  = (annual_pnl  / capital * 100) if capital else 0.0

    lines = [
        f"📊 *VALR PnL Report — {date_str}*",
        "",
        f"{emoji} *Daily Net PnL:* `{total_net:+.4f} USDT`",
        f"   Realised PnL: `{total_rpnl:+.4f}` | Fees: `{total_fees:.4f}` | Positions: `{total_positions}`",
        f"   {fund_emoji} Funding: `{total_funding:+.4f} USDT`",
        "",
    ]

    for s in summaries:
        avg_rate_pct = s["avg_funding_rate"] * 100
        lines.append(
            f"📌 *{s['pair']}:* rPnL `{s['realised_pnl']:+.4f}` | "
            f"funding `{s['funding']:+.4f}` | "
            f"avg rate `{avg_rate_pct:.4f}%`"
        )

    lines += [
        "",
        f"{week_emoji} *7-Day PnL:* `{week_pnl:+.4f} USDT`",
        f"📆 *Inception PnL:* `{cum_net:+.4f} USDT` ({days_tracked} days)",
        f"💰 *Balance:* `{usdt_balance:.2f} USDT` (capital: `{capital:.2f}`)",
        "",
        f"📐 *Run rate* (avg `{avg_daily:+.4f} USDT/day` over {days_tracked}d):",
        f"   Monthly:  `{monthly_pnl:+.4f} USDT`  →  `{monthly_roi:+.2f}%` ROI",
        f"   Annual:   `{annual_pnl:+.4f} USDT`  →  `{annual_roi:+.2f}%` ROI",
        "",
        f"🏆 `{wins}W / {losses}L`  |  "
        f"Best: `{best['net_pnl']:+.4f}` ({best['date']})  |  "
        f"Worst: `{worst['net_pnl']:+.4f}` ({worst['date']})",
    ]

    return "\n".join(lines), ledger, day_record

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    result = subprocess.run(
        ["openclaw", "message", "send",
         "--channel", "telegram",
         "--target", TELEGRAM_CHAT_ID,
         "--message", message],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"Telegram error: {result.stderr}", file=sys.stderr)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now       = datetime.now(GMT8)
    yesterday = (now - timedelta(days=1))
    date_str  = yesterday.strftime("%Y-%m-%d")

    # UTC window for yesterday (GMT+8 day boundaries → UTC)
    # Yesterday 00:00 GMT+8 = yesterday 16:00 UTC the day before
    # Yesterday 23:59:59 GMT+8 = today 15:59:59 UTC
    start_utc = yesterday.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    end_utc   = yesterday.replace(hour=23, minute=59, second=59, microsecond=999000).astimezone(timezone.utc)
    start_iso = start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso   = end_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z")

    print(f"[{now.strftime('%Y-%m-%d %H:%M')} GMT+8] PnL report for {date_str}")
    print(f"UTC window: {start_iso} → {end_iso}")

    txns = fetch_transactions(start_iso, end_iso, GRID_BOT_SUB_ID)
    print(f"Fetched {len(txns)} transactions")

    ledger    = load_ledger()
    summaries = [summarise(txns, pair) for pair in PAIRS]

    for s in summaries:
        print(f"  {s['pair']}: rPnL={s['realised_pnl']:+.6f} | funding={s['funding']:+.6f} | fees={s['trading_fees']:.6f} | net={s['net_pnl']:+.6f}")

    usdt_balance = get_usdt_balance(GRID_BOT_SUB_ID)
    message, ledger, record = build_report(date_str, summaries, usdt_balance, ledger)
    save_ledger(ledger)

    print(f"  Net PnL: {record['net_pnl']:+.4f} USDT | Balance: {usdt_balance:.2f}")
    print("\n--- Message ---")
    print(message)
    print("---\n")

    send_telegram(message)


if __name__ == "__main__":
    main()
