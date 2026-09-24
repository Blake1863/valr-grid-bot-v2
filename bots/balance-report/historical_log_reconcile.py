#!/usr/bin/env python3
"""Build a historical fee/reconciliation example from local bot logs."""

from __future__ import annotations

import argparse
import gzip
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from net_flow import TAKER_FEE_RATE, dec, fmt_cost, fmt_money

ROOT = Path(__file__).resolve().parents[2]
BALANCES = Path(__file__).parent / "balances.json"

SPOT_LOGS = [ROOT / "bots/cm-bot-spot-py/logs/valr-cm-spot.log"]
FUTURES_LOGS = [
    ROOT / "bots/cm-bot-v2/logs/cm-bot-v2.log.4.gz",
    ROOT / "bots/cm-bot-v2/logs/cm-bot-v2.log.3.gz",
    ROOT / "bots/cm-bot-v2/logs/cm-bot-v2.log.2.gz",
    ROOT / "bots/cm-bot-v2/logs/cm-bot-v2.log.1",
    ROOT / "bots/cm-bot-v2/logs/cm-bot-v2.log",
]

TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
SPOT_RE = re.compile(r"\[INFO\] ([A-Z0-9]+): .*PRINT .*~\$(\d+(?:\.\d+)?)")
FUTURES_RE = re.compile(
    r"\[FILL\] \[(CM[12])\] ([A-Z]+USDTPERP) (?:buy|sell) @ "
    r"(\d+(?:\.\d+)?) x (\d+(?:\.\d+)?) \| fee: (\d+(?:\.\d+)?) USDT"
)


def open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", errors="ignore")
    return open(path, errors="ignore")


def in_window(line: str, start: datetime, end: datetime) -> tuple[bool, datetime | None]:
    m = TS_RE.match(line)
    if not m:
        return False, None
    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    return start <= ts < end, ts


def scan_spot(start: datetime, end: datetime) -> dict:
    by_pair = defaultdict(lambda: {"prints": 0, "notional": Decimal("0")})
    total = Decimal("0")
    count = 0
    for path in SPOT_LOGS:
        if not path.exists():
            continue
        with open_text(path) as f:
            for line in f:
                ok, ts = in_window(line, start, end)
                if ts and ts >= end:
                    break
                if not ok:
                    continue
                m = SPOT_RE.search(line)
                if not m:
                    continue
                pair, notional = m.group(1), dec(m.group(2))
                by_pair[pair]["prints"] += 1
                by_pair[pair]["notional"] += notional
                total += notional
                count += 1
    return {"count": count, "notional": total, "by_pair": by_pair}


def scan_futures_file(path: Path) -> dict:
    by_pair = defaultdict(lambda: {"fills": 0, "notional": Decimal("0")})
    total = Decimal("0")
    count = 0
    with open_text(path) as f:
        for line in f:
            m = FUTURES_RE.search(line)
            if not m:
                continue
            _, pair, price, qty, fee = m.groups()
            if dec(fee) <= 0:
                continue
            notional = dec(price) * dec(qty)
            by_pair[pair]["fills"] += 1
            by_pair[pair]["notional"] += notional
            total += notional
            count += 1
    return {"count": count, "notional": total, "by_pair": by_pair}


def scan_futures_prorated(start: datetime, end: datetime) -> dict:
    existing = [p for p in FUTURES_LOGS if p.exists()]
    total = Decimal("0")
    count = Decimal("0")
    by_pair = defaultdict(lambda: {"fills": Decimal("0"), "notional": Decimal("0")})
    files_used = []

    for i, path in enumerate(existing):
        file_end = datetime.fromtimestamp(path.stat().st_mtime)
        file_start = datetime.fromtimestamp(existing[i - 1].stat().st_mtime) if i else file_end - timedelta(days=1)
        overlap_start = max(start, file_start)
        overlap_end = min(end, file_end)
        file_seconds = Decimal(str((file_end - file_start).total_seconds()))
        overlap_seconds = Decimal(str((overlap_end - overlap_start).total_seconds()))
        if file_seconds <= 0 or overlap_seconds <= 0:
            continue

        weight = overlap_seconds / file_seconds
        file_totals = scan_futures_file(path)
        total += file_totals["notional"] * weight
        count += Decimal(file_totals["count"]) * weight
        for pair, row in file_totals["by_pair"].items():
            by_pair[pair]["fills"] += Decimal(row["fills"]) * weight
            by_pair[pair]["notional"] += row["notional"] * weight
        files_used.append((path.name, weight))

    return {"count": count, "notional": total, "by_pair": by_pair, "files_used": files_used}


def snapshot_total(date_key: str) -> Decimal:
    data = json.loads(BALANCES.read_text())
    rows = data["snapshots"][date_key]
    return sum(dec(r.get("balance")) for r in rows)


def account_count(date_key: str) -> int:
    data = json.loads(BALANCES.read_text())
    return len(data["snapshots"][date_key])


def build_report(opening_date: str, closing_date: str, start: datetime, end: datetime) -> str:
    spot = scan_spot(start, end)
    futures = scan_futures_prorated(start, end)
    opening = snapshot_total(opening_date)
    closing = snapshot_total(closing_date)
    spot_fee = spot["notional"] * TAKER_FEE_RATE
    futures_fee = futures["notional"] * TAKER_FEE_RATE
    total_notional = spot["notional"] + futures["notional"]
    total_fee = spot_fee + futures_fee
    movement = closing - opening
    inventory = movement + total_fee

    top_spot = sorted(spot["by_pair"].items(), key=lambda item: item[1]["notional"], reverse=True)[:5]
    top_futures = sorted(futures["by_pair"].items(), key=lambda item: item[1]["notional"], reverse=True)[:5]

    lines = [
        "VALR May 2026 Log-Based Example",
        "",
        f"Snapshot window: {opening_date} -> {closing_date}",
        f"Log window: {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M}",
        f"Accounts in opening/closing snapshots: {account_count(opening_date)} / {account_count(closing_date)}",
        "",
        f"Opening balance: ${opening:,.2f}",
        f"Closing balance: ${closing:,.2f}",
        f"Balance movement: {fmt_money(movement)}",
        "",
        "Fee model: taker notional x 0.02%",
        f"Spot taker notional: {fmt_cost(spot['notional'])} ({spot['count']:,} prints)",
        f"Futures taker notional: {fmt_cost(futures['notional'])} (~{int(futures['count']):,} prorated taker fills)",
        f"Total taker notional: {fmt_cost(total_notional)}",
        f"Estimated fees paid: {fmt_cost(total_fee)}",
        "",
        f"Implied inventory value movement: {fmt_money(inventory)}",
        "Reconcile: opening - fees + inventory movement = closing",
        "",
        "Top spot pairs by notional:",
    ]
    for pair, row in top_spot:
        lines.append(f"{pair}: {fmt_cost(row['notional'])} ({row['prints']:,} prints)")
    lines.append("")
    lines.append("Top futures pairs by taker notional (prorated):")
    for pair, row in top_futures:
        lines.append(f"{pair}: {fmt_cost(row['notional'])} (~{int(row['fills']):,} fills)")
    lines.append("")
    lines.append("Note: spot is exact from timestamped PRINT lines; futures is prorated from rotated logs because those files have no per-line timestamps.")
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--opening-date", default="2026-05-05")
    parser.add_argument("--closing-date", default="2026-05-31")
    parser.add_argument("--start", default="2026-05-05T05:00:00")
    parser.add_argument("--end", default="2026-06-01T05:00:00")
    return parser.parse_args()


def main():
    args = parse_args()
    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    print(build_report(args.opening_date, args.closing_date, start, end))


if __name__ == "__main__":
    main()
