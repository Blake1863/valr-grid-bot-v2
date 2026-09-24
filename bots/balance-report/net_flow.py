#!/usr/bin/env python3
"""Shared VALR balance and net-flow accounting helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

import requests

BASE_URL = "https://api.valr.com"
HERE = Path(__file__).parent
BALANCE_LEDGER_FILE = HERE / "balances.json"
FLOW_LEDGER_FILE = HERE / "net_flows.json"
ENV_PATH = HERE.parent / "cm-bot-spot" / ".env"

ZERO = Decimal("0")
TAKER_FEE_RATE = Decimal("0.0002")
STABLES = {"USDT", "USDC", "USD"}
TRADE_TYPES = {
    "LIMIT_BUY",
    "LIMIT_SELL",
    "MARKET_BUY",
    "MARKET_SELL",
    "SIMPLE_BUY",
    "SIMPLE_SELL",
    "SIMPLE_SWAP_BUY",
    "SIMPLE_SWAP_SELL",
    "AUTO_BUY",
    "TRADE",
}
MAKER_REWARD_TYPES = {"MAKER_REWARD"}
FUNDING_EARNED_TYPES = {"FUTURES_FUNDING_EARNED"}
FUNDING_PAID_TYPES = {"FUTURES_FUNDING_PAID"}
INTEREST_TYPES = {"SPOT_BORROW_INTEREST_CHARGE"}
EXTERNAL_IN_TYPES = {
    "BLOCKCHAIN_RECEIVE",
    "FIAT_DEPOSIT",
    "CREDIT_CARD_DEPOSIT",
    "OFF_CHAIN_BLOCKCHAIN_DEPOSIT",
    "PAYMENT_RECEIVED",
    "REFERRAL_REBATE",
    "REFERRAL_REWARD",
    "PROMOTIONAL_REBATE",
    "PAYMENT_REWARD",
}
EXTERNAL_OUT_TYPES = {
    "BLOCKCHAIN_SEND",
    "FIAT_WITHDRAWAL",
    "OFF_CHAIN_BLOCKCHAIN_WITHDRAW",
    "PAYMENT_SENT",
}
TRANSFER_TYPES = {"INTERNAL_TRANSFER"}


@dataclass
class AccountSnapshot:
    label: str
    sub_id: str
    balance: Decimal | None
    components: dict[str, Decimal]
    tx_count: int
    warnings: list[str]


def dec(value) -> Decimal:
    if value in (None, ""):
        return ZERO
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return ZERO


def money(value: Decimal | float | int | None, places: str = "0.0001") -> float | None:
    if value is None:
        return None
    return float(Decimal(value).quantize(Decimal(places)))


def load_env() -> dict[str, str]:
    creds: dict[str, str] = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip().strip('"').strip("'")
    return creds


def headers(verb: str, path: str, body: str = "", sub_id: str = "") -> dict[str, str]:
    creds = load_env()
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{verb}{path}{body}{sub_id}"
    sig = hmac.new(creds["MAIN_API_SECRET"].encode(), msg.encode(), hashlib.sha512).hexdigest()
    h = {
        "X-VALR-API-KEY": creds["MAIN_API_KEY"],
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
    }
    if sub_id:
        h["X-VALR-SUB-ACCOUNT-ID"] = sub_id
    return h


def api_get(path: str, sub_id: str = "", timeout: int = 30):
    r = requests.get(BASE_URL + path, headers=headers("GET", path, "", sub_id), timeout=timeout)
    r.raise_for_status()
    return r.json()


def get_subaccounts() -> list[dict]:
    subs = api_get("/v1/account/subaccounts")
    primary = [s for s in subs if str(s["id"]) == "0"]
    others = sorted([s for s in subs if str(s["id"]) != "0"], key=lambda s: s["label"])
    return primary + others


def get_reference_balance(sub_id: str = "") -> Decimal:
    balances = api_get("/v1/account/balances?excludeZeroBalances=true", sub_id=sub_id)
    return sum(dec(b.get("totalInReference")) for b in balances)


def load_json(path: Path, default: dict) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def load_balance_ledger() -> dict:
    return load_json(BALANCE_LEDGER_FILE, {"snapshots": {}})


def load_flow_ledger() -> dict:
    return load_json(FLOW_LEDGER_FILE, {"days": {}})


def save_balance_ledger(ledger: dict):
    save_json(BALANCE_LEDGER_FILE, ledger)


def save_flow_ledger(ledger: dict):
    save_json(FLOW_LEDGER_FILE, ledger)


def infer_snapshot_time(date_key: str) -> datetime:
    return datetime.combine(datetime.fromisoformat(date_key).date(), dt_time(5, 0), tzinfo=timezone.utc)


def previous_snapshot(balance_ledger: dict, date_key: str) -> tuple[str | None, datetime | None, Decimal | None]:
    keys = sorted(k for k in balance_ledger.get("snapshots", {}) if k < date_key)
    if not keys:
        return None, None, None
    prev_key = keys[-1]
    rows = balance_ledger["snapshots"][prev_key]
    generated_at = None
    for row in rows:
        if row.get("generated_at"):
            generated_at = datetime.fromisoformat(row["generated_at"].replace("Z", "+00:00"))
            break
    return prev_key, generated_at or infer_snapshot_time(prev_key), sum(dec(r.get("balance")) for r in rows)


def fetch_transactions(sub_id: str, start: datetime, end: datetime) -> list[dict]:
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    end_iso = end.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    txns: list[dict] = []
    before_id = ""
    seen_cursors: set[str] = set()

    while True:
        path = (
            "/v1/account/transactionhistory"
            f"?limit=200&startTime={quote(start_iso)}&endTime={quote(end_iso)}"
        )
        if before_id:
            path += f"&beforeId={quote(before_id)}"
        batch = api_get(path, sub_id=sub_id)
        if not batch:
            break
        txns.extend(batch)
        if len(batch) < 200:
            break
        if batch[-1]["id"] in seen_cursors:
            break
        before_id = batch[-1]["id"]
        seen_cursors.add(before_id)
        time.sleep(0.2)
    return txns


def value_from_trade_sides(tx: dict, currency: str, amount: Decimal) -> Decimal | None:
    debit_ccy = tx.get("debitCurrency")
    credit_ccy = tx.get("creditCurrency")
    debit = dec(tx.get("debitValue"))
    credit = dec(tx.get("creditValue"))

    if currency in STABLES:
        return amount
    if currency == credit_ccy and credit and debit_ccy in STABLES:
        return amount * debit / credit
    if currency == debit_ccy and debit and credit_ccy in STABLES:
        return amount * credit / debit
    return None


class Converter:
    def __init__(self):
        self.pairs: list[dict] | None = None
        self.rates: dict[tuple[str, str], Decimal | None] = {}

    def load_pairs(self):
        if self.pairs is None:
            self.pairs = api_get("/v1/public/pairs")

    def to_reference(self, currency: str | None, amount: Decimal, tx: dict | None = None) -> Decimal:
        if not currency or not amount:
            return ZERO
        if currency in STABLES:
            return amount
        if tx:
            side_value = value_from_trade_sides(tx, currency, amount)
            if side_value is not None:
                return side_value

        for target in ("USDT", "USDC"):
            rate = self.rate(currency, target)
            if rate:
                return amount * rate
        return ZERO

    def rate(self, source: str, target: str) -> Decimal | None:
        key = (source, target)
        if key in self.rates:
            return self.rates[key]

        self.load_pairs()
        assert self.pairs is not None
        direct = next(
            (
                p
                for p in self.pairs
                if p.get("baseCurrency") == source and p.get("quoteCurrency") == target
            ),
            None,
        )
        inverse = next(
            (
                p
                for p in self.pairs
                if p.get("baseCurrency") == target and p.get("quoteCurrency") == source
            ),
            None,
        )

        pair = direct or inverse
        if not pair:
            self.rates[key] = None
            return None
        summary = api_get(f"/v1/public/{pair['symbol']}/marketsummary")
        price = dec(summary.get("lastTradedPrice") or summary.get("markPrice"))
        if not price:
            self.rates[key] = None
        elif direct:
            self.rates[key] = price
        else:
            self.rates[key] = Decimal("1") / price
        time.sleep(0.1)
        return self.rates[key]


def empty_components() -> dict[str, Decimal]:
    return {
        "taker_notional_ref": ZERO,
        "trade_fees_ref": ZERO,
        "maker_rewards_ref": ZERO,
        "funding_net_ref": ZERO,
        "borrow_interest_ref": ZERO,
        "external_flow_ref": ZERO,
        "net_fee_effect_ref": ZERO,
    }


def classify_transactions(txns: list[dict], converter: Converter) -> tuple[dict[str, Decimal], list[str]]:
    components = empty_components()
    warnings: list[str] = []

    for tx in txns:
        tx_type = tx.get("transactionType", {}).get("type", "")
        fee = dec(tx.get("feeValue"))
        if tx_type in TRADE_TYPES and fee:
            notional_ref = trade_notional_ref(tx, converter)
            components["taker_notional_ref"] += notional_ref
            components["trade_fees_ref"] += notional_ref * TAKER_FEE_RATE
            if not notional_ref:
                warnings.append(f"unvalued taker notional on {tx.get('id')}")

        if tx_type in MAKER_REWARD_TYPES:
            components["maker_rewards_ref"] += converter.to_reference(
                tx.get("creditCurrency"), dec(tx.get("creditValue")), tx
            )
        elif tx_type in FUNDING_EARNED_TYPES:
            components["funding_net_ref"] += converter.to_reference(
                tx.get("creditCurrency") or "USDT", dec(tx.get("creditValue")), tx
            )
        elif tx_type in FUNDING_PAID_TYPES:
            components["funding_net_ref"] -= converter.to_reference(
                tx.get("debitCurrency") or "USDT", dec(tx.get("debitValue")), tx
            )
        elif tx_type in INTEREST_TYPES:
            components["borrow_interest_ref"] += converter.to_reference(
                tx.get("debitCurrency"), dec(tx.get("debitValue")), tx
            )
        elif tx_type in EXTERNAL_IN_TYPES:
            components["external_flow_ref"] += converter.to_reference(
                tx.get("creditCurrency"), dec(tx.get("creditValue")), tx
            )
        elif tx_type in EXTERNAL_OUT_TYPES:
            components["external_flow_ref"] -= converter.to_reference(
                tx.get("debitCurrency"), dec(tx.get("debitValue")), tx
            )
        elif tx_type in TRANSFER_TYPES:
            components["external_flow_ref"] += converter.to_reference(
                tx.get("creditCurrency"), dec(tx.get("creditValue")), tx
            )
            components["external_flow_ref"] -= converter.to_reference(
                tx.get("debitCurrency"), dec(tx.get("debitValue")), tx
            )

    components["net_fee_effect_ref"] = (
        components["maker_rewards_ref"]
        + components["funding_net_ref"]
        - components["trade_fees_ref"]
        - components["borrow_interest_ref"]
    )
    return components, warnings


def trade_notional_ref(tx: dict, converter: Converter) -> Decimal:
    debit_currency = tx.get("debitCurrency")
    credit_currency = tx.get("creditCurrency")
    debit_value = dec(tx.get("debitValue"))
    credit_value = dec(tx.get("creditValue"))

    if debit_currency in STABLES:
        return debit_value
    if credit_currency in STABLES:
        return credit_value
    debit_ref = converter.to_reference(debit_currency, debit_value, tx)
    if debit_ref:
        return debit_ref
    return converter.to_reference(credit_currency, credit_value, tx)


def snapshot_accounts(period_start: datetime, period_end: datetime) -> list[AccountSnapshot]:
    converter = Converter()
    rows: list[AccountSnapshot] = []
    for sub in get_subaccounts():
        label = sub["label"]
        sid = str(sub["id"])
        sub_id = "" if sid == "0" else sid
        print(f"  fetching {label}...", flush=True)
        warnings: list[str] = []
        try:
            balance = get_reference_balance(sub_id)
        except Exception as exc:
            balance = None
            warnings.append(f"balance fetch failed: {exc}")

        try:
            txns = fetch_transactions(sub_id, period_start, period_end)
            components, tx_warnings = classify_transactions(txns, converter)
            warnings.extend(tx_warnings)
        except Exception as exc:
            txns = []
            components = empty_components()
            warnings.append(f"transaction fetch failed: {exc}")

        rows.append(
            AccountSnapshot(
                label=label,
                sub_id=sid,
                balance=balance,
                components=components,
                tx_count=len(txns),
                warnings=warnings,
            )
        )
        time.sleep(0.15)
    return rows


def aggregate_components(rows: list[AccountSnapshot]) -> dict[str, Decimal]:
    agg = empty_components()
    for row in rows:
        for key in agg:
            agg[key] += row.components.get(key, ZERO)
    return agg


def write_daily_ledgers(
    date_key: str,
    generated_at: datetime,
    period_start: datetime,
    period_end: datetime,
    rows: list[AccountSnapshot],
    balance_ledger: dict,
    flow_ledger: dict,
    previous_date: str | None = None,
    previous_total: Decimal | None = None,
) -> dict:
    balance_rows = [
        {
            "label": row.label,
            "sub_id": row.sub_id,
            "balance": money(row.balance),
            "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        }
        for row in rows
    ]
    balance_ledger.setdefault("snapshots", {})[date_key] = balance_rows

    total_balance = sum((row.balance or ZERO) for row in rows)
    agg = aggregate_components(rows)
    prev_key, _, inferred_prev_total = previous_snapshot(balance_ledger, date_key)
    if previous_date is None:
        prev_key = None
    else:
        prev_key = previous_date
    prev_total = previous_total
    if prev_total is None:
        prev_total = inferred_prev_total if previous_date is not None else None
    balance_delta = None if prev_total is None else total_balance - prev_total
    inventory_move = None
    if balance_delta is not None:
        inventory_move = balance_delta - agg["external_flow_ref"] - agg["net_fee_effect_ref"]

    day = {
        "date": date_key,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "period_start": period_start.isoformat().replace("+00:00", "Z"),
        "period_end": period_end.isoformat().replace("+00:00", "Z"),
        "previous_snapshot_date": prev_key,
        "total_balance_ref": money(total_balance),
        "balance_delta_ref": money(balance_delta) if balance_delta is not None else None,
        "inventory_value_movement_ref": money(inventory_move) if inventory_move is not None else None,
        "components": {k: money(v) for k, v in agg.items()},
        "accounts": [
            {
                "label": row.label,
                "sub_id": row.sub_id,
                "balance_ref": money(row.balance),
                "tx_count": row.tx_count,
                "components": {k: money(v) for k, v in row.components.items()},
                "warnings": row.warnings,
            }
            for row in rows
        ],
    }
    flow_ledger.setdefault("days", {})[date_key] = day
    save_balance_ledger(balance_ledger)
    save_flow_ledger(flow_ledger)
    return day


def fmt_money(value) -> str:
    if value is None:
        return "n/a"
    value = Decimal(str(value))
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def fmt_cost(value) -> str:
    if value is None:
        return "n/a"
    return f"${abs(Decimal(str(value))):,.2f}"
