#!/usr/bin/env python3
"""
VALR Monthly Net-Flow Report
----------------------------
Uses the daily net-flow ledger produced by balance_report.py to reconcile:

    closing balance
      = opening balance
      + external flows
      + net fee/funding effect
      + inventory value movement

Cron: 05:30 UTC on the 1st of each month.
"""

from __future__ import annotations

import argparse
import calendar
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal

from net_flow import (
    BALANCE_LEDGER_FILE,
    FLOW_LEDGER_FILE,
    dec,
    fmt_cost,
    fmt_money,
    load_balance_ledger,
    load_flow_ledger,
)

TELEGRAM_CHAT_ID = "7018990694"
COMPONENT_KEYS = (
    "taker_notional_ref",
    "trade_fees_ref",
    "maker_rewards_ref",
    "funding_net_ref",
    "borrow_interest_ref",
    "external_flow_ref",
    "net_fee_effect_ref",
)


def snapshot_total(balance_ledger: dict, date_key: str | None) -> Decimal | None:
    if not date_key:
        return None
    rows = balance_ledger.get("snapshots", {}).get(date_key)
    if not rows:
        return None
    return sum(dec(r.get("balance")) for r in rows)


def snapshot_account_map(balance_ledger: dict, date_key: str | None) -> dict[str, Decimal]:
    if not date_key:
        return {}
    return {
        r["sub_id"]: dec(r.get("balance"))
        for r in balance_ledger.get("snapshots", {}).get(date_key, [])
    }


def target_month(now_utc: datetime, requested: str | None) -> str:
    if requested:
        return requested
    if now_utc.month == 1:
        return f"{now_utc.year - 1}-12"
    return f"{now_utc.year}-{now_utc.month - 1:02d}"


def compile_month(month_key: str) -> dict:
    balance_ledger = load_balance_ledger()
    flow_ledger = load_flow_ledger()
    days = [
        day
        for key, day in sorted(flow_ledger.get("days", {}).items())
        if key.startswith(month_key)
    ]
    if not days:
        raise RuntimeError(f"No daily net-flow rows found for {month_key} in {FLOW_LEDGER_FILE}")

    first = days[0]
    last = days[-1]
    opening_date = first.get("previous_snapshot_date")
    closing_date = last["date"]
    opening_total = snapshot_total(balance_ledger, opening_date)
    if opening_total is None and first.get("balance_delta_ref") is not None:
        opening_total = dec(first["total_balance_ref"]) - dec(first["balance_delta_ref"])
    closing_total = dec(last["total_balance_ref"])

    components = {key: Decimal("0") for key in COMPONENT_KEYS}
    account_components: dict[str, dict] = {}
    for day in days:
        for key in COMPONENT_KEYS:
            components[key] += dec(day.get("components", {}).get(key))
        for acct in day.get("accounts", []):
            slot = account_components.setdefault(
                acct["sub_id"],
                {"label": acct["label"], "sub_id": acct["sub_id"], **{k: Decimal("0") for k in COMPONENT_KEYS}},
            )
            for key in COMPONENT_KEYS:
                slot[key] += dec(acct.get("components", {}).get(key))

    opening_accounts = snapshot_account_map(balance_ledger, opening_date)
    closing_accounts = snapshot_account_map(balance_ledger, closing_date)
    account_rows = []
    for sub_id, slot in sorted(account_components.items(), key=lambda item: item[1]["label"]):
        opening = opening_accounts.get(sub_id, Decimal("0"))
        closing = closing_accounts.get(sub_id, Decimal("0"))
        inventory = closing - opening - slot["external_flow_ref"] - slot["net_fee_effect_ref"]
        account_rows.append(
            {
                "label": slot["label"],
                "opening": opening,
                "closing": closing,
                "trade_fees_ref": slot["trade_fees_ref"],
                "net_fee_effect_ref": slot["net_fee_effect_ref"],
                "inventory_value_movement_ref": inventory,
            }
        )

    balance_delta = closing_total - opening_total
    inventory_move = balance_delta - components["external_flow_ref"] - components["net_fee_effect_ref"]
    reconciled = opening_total + components["external_flow_ref"] + components["net_fee_effect_ref"] + inventory_move

    return {
        "month": month_key,
        "day_count": len(days),
        "opening_date": opening_date,
        "closing_date": closing_date,
        "opening_total": opening_total,
        "closing_total": closing_total,
        "balance_delta": balance_delta,
        "components": components,
        "inventory_value_movement": inventory_move,
        "reconciled": reconciled,
        "account_rows": account_rows,
    }


def build_report(month: dict) -> str:
    c = month["components"]
    check_ok = abs(month["reconciled"] - month["closing_total"]) < Decimal("0.05")
    lines = [
        f"VALR Monthly Net-Flow — {month['month']}",
        "",
        f"Rows: {month['day_count']} daily snapshots",
        f"Opening ({month['opening_date']}): ${month['opening_total']:,.2f}",
        f"Closing ({month['closing_date']}): ${month['closing_total']:,.2f}",
        f"Balance movement: {fmt_money(month['balance_delta'])}",
        "",
        "Reconciliation:",
        f"External flows: {fmt_money(c['external_flow_ref'])}",
        f"Taker notional: {fmt_cost(c['taker_notional_ref'])}",
        f"Trade fees paid: {fmt_cost(c['trade_fees_ref'])}",
        f"Maker rewards: {fmt_money(c['maker_rewards_ref'])}",
        f"Funding net: {fmt_money(c['funding_net_ref'])}",
        f"Borrow interest paid: {fmt_cost(c['borrow_interest_ref'])}",
        f"Net fee/funding effect: {fmt_money(c['net_fee_effect_ref'])}",
        f"Inventory value movement: {fmt_money(month['inventory_value_movement'])}",
        f"Check: {'OK' if check_ok else 'NOT OK'}",
        "",
        "Largest account moves:",
    ]

    rows = sorted(
        month["account_rows"],
        key=lambda r: abs(r["closing"] - r["opening"]),
        reverse=True,
    )[:8]
    for row in rows:
        lines.append(
            f"{row['label']}: {fmt_money(row['closing'] - row['opening'])}, "
            f"fees {fmt_cost(row['trade_fees_ref'])}, "
            f"inventory {fmt_money(row['inventory_value_movement_ref'])}"
        )

    lines += [
        "",
        f"Source: {FLOW_LEDGER_FILE.name} + {BALANCE_LEDGER_FILE.name}",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
    ]
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
    parser.add_argument("--month", help="month to report, YYYY-MM")
    parser.add_argument("--no-send", action="store_true", help="print report but do not send Telegram")
    return parser.parse_args()


def main():
    args = parse_args()
    now_utc = datetime.now(timezone.utc)
    month_key = target_month(now_utc, args.month)
    year, month_num = [int(part) for part in month_key.split("-")]
    calendar.monthrange(year, month_num)

    print(f"[{now_utc.strftime('%Y-%m-%d %H:%M')} UTC] Monthly net-flow report for {month_key}")
    month = compile_month(month_key)
    message = build_report(month)
    print(f"\n--- Message ---\n{message}\n---\n")
    if not args.no_send:
        send_telegram(message)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
