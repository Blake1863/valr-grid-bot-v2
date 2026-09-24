#!/usr/bin/env python3
"""
VALR Daily Balance + Net-Flow Report
------------------------------------
Snapshots all account balances, records daily realized fees/rewards/funding
from VALR transaction history, and stores a net-flow ledger for month-end
reconciliation.

Cron: 05:00 UTC (= 13:00 SGT)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone

from net_flow import (
    fmt_cost,
    fmt_money,
    load_balance_ledger,
    load_flow_ledger,
    previous_snapshot,
    snapshot_accounts,
    write_daily_ledgers,
)

TELEGRAM_CHAT_ID = "7018990694"


def build_report(day: dict) -> str:
    components = day["components"]
    warnings = [
        f"{acct['label']}: {warning}"
        for acct in day["accounts"]
        for warning in acct.get("warnings", [])
    ]

    lines = [
        f"VALR Daily Net-Flow — {day['date']}",
        "",
        f"Balance: ${day['total_balance_ref']:,.2f} USDC-ref",
    ]

    if day.get("balance_delta_ref") is not None:
        lines.append(f"Balance change: {fmt_money(day['balance_delta_ref'])}")
    if day.get("inventory_value_movement_ref") is not None:
        lines.append(f"Inventory value movement: {fmt_money(day['inventory_value_movement_ref'])}")

    lines += [
        "",
        "Realized components:",
        f"Taker notional: {fmt_cost(components['taker_notional_ref'])}",
        f"Trade fees paid: {fmt_cost(components['trade_fees_ref'])}",
        f"Maker rewards: {fmt_money(components['maker_rewards_ref'])}",
        f"Funding net: {fmt_money(components['funding_net_ref'])}",
        f"Borrow interest paid: {fmt_cost(components['borrow_interest_ref'])}",
        f"External flow: {fmt_money(components['external_flow_ref'])}",
        f"Net fee/funding effect: {fmt_money(components['net_fee_effect_ref'])}",
        "",
        f"Period: {day['period_start']} to {day['period_end']}",
    ]

    if warnings:
        lines += ["", f"Warnings: {len(warnings)} valuation/fetch issue(s). Check net_flows.json."]

    return "\n".join(lines)


def send_telegram(message: str):
    result = subprocess.run(
        [
            "openclaw",
            "message",
            "send",
            "--channel",
            "telegram",
            "--target",
            TELEGRAM_CHAT_ID,
            "--message",
            message,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        print(f"Telegram error: {result.stderr}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-send", action="store_true", help="write ledgers but do not send Telegram")
    return parser.parse_args()


def main():
    args = parse_args()
    now_utc = datetime.now(timezone.utc)
    date_key = now_utc.strftime("%Y-%m-%d")

    print(f"[{now_utc.strftime('%Y-%m-%d %H:%M')} UTC] Daily net-flow report starting")
    balance_ledger = load_balance_ledger()
    flow_ledger = load_flow_ledger()
    prev_key, prev_time, prev_total = previous_snapshot(balance_ledger, date_key)
    period_start = prev_time or now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    if (now_utc - period_start).total_seconds() > 36 * 60 * 60:
        print("Previous snapshot is too old for a clean daily fee period; bootstrapping from 00:00 UTC.")
        prev_key = None
        prev_total = None
        period_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    print(f"Period: {period_start.isoformat()} to {now_utc.isoformat()} (previous snapshot {prev_key})", flush=True)

    rows = snapshot_accounts(period_start, now_utc)
    for row in rows:
        status = "ERROR" if row.balance is None else f"${row.balance:,.4f}"
        print(f"  {row.label}: {status}, tx={row.tx_count}, warnings={len(row.warnings)}")

    day = write_daily_ledgers(
        date_key,
        now_utc,
        period_start,
        now_utc,
        rows,
        balance_ledger,
        flow_ledger,
        previous_date=prev_key,
        previous_total=prev_total,
    )
    message = build_report(day)
    print(f"\n--- Message ---\n{message}\n---\n")
    if not args.no_send:
        send_telegram(message)


if __name__ == "__main__":
    main()
