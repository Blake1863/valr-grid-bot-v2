#!/usr/bin/env python3
"""
Monthly Trading Reconciliation
Runs on the 1st of each month via cron.
Fetches balances and fee/funding transactions across CM1, CM2, CMS1, CMS2
via the VALR transaction history API, produces a reconciliation table,
and sends it via Telegram.
"""

import calendar
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ─── Config ─────────────────────────────────────────────────────────────────

BASE_URL   = "https://api.valr.com"
STATE_FILE = Path("/home/admin/.openclaw/workspace/bots/cm-bot-v2/scripts/recon_state.json")

MAIN_API_KEY    = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
MAIN_API_SECRET = "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"

SUBACCOUNTS = {
    "CM1":  "1483472097578319872",
    "CM2":  "1483472079069155328",
    "CMS1": "1483815480334401536",
    "CMS2": "1483815498551132160",
}

TELEGRAM_BOT_TOKEN = "8749538904:AAFBkGh0e_bxs1uhOAucdj09A-QW_S-3twg"
TELEGRAM_CHAT_ID   = "7018990694"

# Transaction types we care about for fee/cost accounting
# NOTE: Spot trade feeValue is in the BASE currency (not USDT) so we cannot
# sum it directly. Instead we derive the implicit spread cost from debit/credit
# values converted to USDT reference, OR we use MAKER_REWARD for rebates.
# Futures fees are separate explicit USDT line items — safe to sum directly.
FEE_TX_TYPES = [
    "FUTURES_TRADE_FEE",       # futures taker/maker fees (explicit USDT line item)
    "FUTURES_FUNDING_EARNED",  # positive: received funding
    "FUTURES_FUNDING_PAID",    # negative: paid funding
    "MAKER_REWARD",            # spot maker rebates (creditCurrency = USDT or base)
]

# Spot trade types — used to count volume but NOT for fee extraction
# (fees are embedded in debitValue/creditValue, denominated in base asset)
SPOT_TRADE_TYPES = [
    "LIMIT_BUY", "LIMIT_SELL", "MARKET_BUY", "MARKET_SELL",
    "SIMPLE_BUY", "SIMPLE_SELL",
]

ALL_FETCH_TYPES = SPOT_TRADE_TYPES + FEE_TX_TYPES


# ─── VALR Auth ───────────────────────────────────────────────────────────────

def _sign(timestamp: str, verb: str, path: str, body: str = "", sub_id: str = "") -> str:
    msg = f"{timestamp}{verb}{path}{body}{sub_id}"
    return hmac.new(MAIN_API_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()


def _headers(verb: str, path: str, body: str = "", sub_id: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "X-VALR-API-KEY":        MAIN_API_KEY,
        "X-VALR-SIGNATURE":      _sign(ts, verb, path, body, sub_id),
        "X-VALR-TIMESTAMP":      ts,
        "X-VALR-SUB-ACCOUNT-ID": sub_id,
        "Content-Type":          "application/json",
    }


def _get(path: str, sub_id: str = "") -> list | dict:
    url = BASE_URL + path
    r = requests.get(url, headers=_headers("GET", path, "", sub_id), timeout=20)
    r.raise_for_status()
    return r.json()


# ─── Transaction History (paginated) ─────────────────────────────────────────

def fetch_transactions(sub_id: str, label: str, start_iso: str, end_iso: str) -> list:
    """
    Fetch all transactions of relevant types for the given period,
    paginating via beforeId until exhausted.
    """
    types_param = ",".join(ALL_FETCH_TYPES)
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

        try:
            batch = _get(path, sub_id)
        except Exception as e:
            print(f"  [WARN] Transaction fetch failed for {label}: {e}")
            break

        if not batch:
            break

        all_txns.extend(batch)

        if len(batch) < 200:
            # Last page
            break

        # Paginate: use last ID in batch
        before_id = batch[-1]["id"]
        time.sleep(0.3)  # be polite

    print(f"  {label}: {len(all_txns)} transactions fetched")
    return all_txns


# ─── Fee Calculation ──────────────────────────────────────────────────────────

def calc_fees(txns: list) -> dict:
    """
    Returns a dict with fee components (all in USDT/USDC reference):

      spot_fees     - derived: for each spot trade, fee = debitValue * fee_rate
                      We use 0.02% taker rate (2bps) applied to USDT debit.
                      feeValue is in BASE currency — do NOT use it directly.
      futures_fees  - FUTURES_TRADE_FEE: explicit USDT debit/credit line items
      funding_net   - FUTURES_FUNDING_EARNED minus FUTURES_FUNDING_PAID
      maker_rewards - MAKER_REWARD creditValue (only if creditCurrency is USDT/USDC)
      spot_volume   - total USDT debited on spot trades (informational)
      total_net     - net fee cost (negative = cost paid, positive = net rebate)
    """
    SPOT_TAKER_RATE = 0.0002  # 2bps taker fee

    spot_fees     = 0.0
    spot_volume   = 0.0
    futures_fees  = 0.0
    funding_net   = 0.0
    maker_rewards = 0.0

    for tx in txns:
        tx_type    = tx.get("transactionType", {}).get("type", "")
        credit     = float(tx.get("creditValue") or 0)
        debit      = float(tx.get("debitValue") or 0)
        credit_ccy = tx.get("creditCurrency", "")
        debit_ccy  = tx.get("debitCurrency", "")

        if tx_type in SPOT_TRADE_TYPES:
            # Fee is embedded in debitValue (USDT side of the trade)
            # Approximate: fee = USDT notional * taker rate
            if debit_ccy in ("USDT", "USDC"):
                fee_est = debit * SPOT_TAKER_RATE
                spot_fees -= fee_est
                spot_volume += debit
            elif credit_ccy in ("USDT", "USDC"):
                # SELL — credit is USDT received
                fee_est = credit * SPOT_TAKER_RATE
                spot_fees -= fee_est
                spot_volume += credit

        elif tx_type == "FUTURES_TRADE_FEE":
            # Explicit USDT fee line — debit = cost paid, credit = rebate received
            futures_fees += credit - debit

        elif tx_type == "FUTURES_FUNDING_EARNED":
            funding_net += credit

        elif tx_type == "FUTURES_FUNDING_PAID":
            funding_net -= debit

        elif tx_type == "MAKER_REWARD":
            # Only count if reward is in a stablecoin (not base asset)
            if credit_ccy in ("USDT", "USDC", ""):
                maker_rewards += credit

    total_net = spot_fees + futures_fees + funding_net + maker_rewards
    return {
        "spot_fees":     spot_fees,
        "spot_volume":   spot_volume,
        "futures_fees":  futures_fees,
        "funding_net":   funding_net,
        "maker_rewards": maker_rewards,
        "total_net":     total_net,
    }


# ─── Balance ─────────────────────────────────────────────────────────────────

def get_total_balance(sub_id: str, label: str) -> float:
    """
    Returns total USDC-reference balance for a subaccount.
    Uses totalInReference for non-stablecoins, direct value for USDT/USDC.
    """
    try:
        balances = _get("/v1/account/balances", sub_id)
        total = 0.0
        for b in balances:
            currency = b.get("currency", "")
            if currency in ("USDT", "USDC"):
                total += float(b.get("total", 0) or 0)
            elif b.get("totalInReference"):
                total += float(b["totalInReference"])
        return total
    except Exception as e:
        print(f"  [WARN] Balance fetch failed for {label} ({sub_id}): {e}")
        return 0.0


# ─── State ────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "Markdown",
    }, timeout=15)
    r.raise_for_status()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc)

    # Prior month date range
    if now.month == 1:
        pm_year, pm_month = now.year - 1, 12
    else:
        pm_year, pm_month = now.year, now.month - 1

    last_day    = calendar.monthrange(pm_year, pm_month)[1]
    start_iso   = f"{pm_year}-{pm_month:02d}-01T00:00:00.000Z"
    end_iso     = f"{pm_year}-{pm_month:02d}-{last_day:02d}T23:59:59.999Z"
    month_label = f"{pm_year}-{pm_month:02d}"

    print(f"[{now.isoformat()}] Reconciliation for {month_label}")
    print(f"Period: {start_iso} → {end_iso}\n")

    state = load_state()

    # ── Closing balances ──
    print("Fetching closing balances...")
    closing = {}
    for label, sub_id in SUBACCOUNTS.items():
        closing[label] = get_total_balance(sub_id, label)
        print(f"  {label}: ${closing[label]:,.2f}")
    total_closing = sum(closing.values())

    # ── Opening balance ──
    opening_key = f"closing_{month_label}"
    if opening_key in state:
        total_opening = state[opening_key]
    else:
        print(f"[INFO] No prior state — bootstrapping with current closing balance.")
        total_opening = total_closing

    print(f"\nOpening balance: ${total_opening:,.2f}")
    print(f"Closing balance: ${total_closing:,.2f}\n")

    # ── Fees via transaction history ──
    print("Fetching transaction history for fees...")
    fee_components = {label: {"spot_fees": 0, "futures_fees": 0, "funding_net": 0, "maker_rewards": 0, "total_net": 0}
                      for label in SUBACCOUNTS}

    for label, sub_id in SUBACCOUNTS.items():
        txns = fetch_transactions(sub_id, label, start_iso, end_iso)
        fee_components[label] = calc_fees(txns)

    # Aggregate fees
    agg = {k: sum(fee_components[lbl][k] for lbl in SUBACCOUNTS)
           for k in ("spot_fees", "spot_volume", "futures_fees", "funding_net", "maker_rewards", "total_net")}

    print(f"\nFee breakdown:")
    print(f"  Spot volume:   ${agg['spot_volume']:,.2f} USDT")
    print(f"  Spot fees:     ${agg['spot_fees']:,.4f} (est @2bps)")
    print(f"  Futures fees:  ${agg['futures_fees']:,.4f}")
    print(f"  Funding net:   ${agg['funding_net']:,.4f}")
    print(f"  Maker rewards: ${agg['maker_rewards']:,.4f}")
    print(f"  Total net:     ${agg['total_net']:,.4f}")

    total_fees = agg["total_net"]

    # ── Inventory change ──
    # Opening + fees_net + inventory_change = Closing
    inventory_change = total_closing - total_opening - total_fees
    inv_label = "Gain" if inventory_change >= 0 else "Loss"

    # ── Verify ──
    check = total_opening + total_fees + inventory_change
    check_ok = abs(check - total_closing) < 0.01

    # ── Format table ──
    def f(v: float) -> str:
        s = "+" if v >= 0 else ""
        return f"{s}${v:,.2f}"

    msg = (
        f"*📊 Monthly Reconciliation — {month_label}*\n\n"
        f"```\n"
        f"{'#':<3} {'Item':<32} {'Amount':>12}\n"
        f"{'─'*3} {'─'*32} {'─'*12}\n"
        f"{'1':<3} {'Opening Balance (USDC ref)':<32} {'${:,.2f}'.format(total_opening):>12}\n"
        f"{'2':<3} {'Total Fees (net)':<32} {f(total_fees):>12}\n"
        f"{'3':<3} {'Inventory ' + inv_label:<32} {f(inventory_change):>12}\n"
        f"{'4':<3} {'Closing Balance (USDC ref)':<32} {'${:,.2f}'.format(total_closing):>12}\n"
        f"```\n\n"
        f"{'✅' if check_ok else '⚠️'} Check: {f(check)} {'= Closing ✓' if check_ok else '≠ Closing ✗'}\n\n"
        f"*Fee detail (all accounts):*\n"
        f"  Spot volume: `${agg['spot_volume']:,.2f}` | fees est @2bps: `{f(agg['spot_fees'])}`\n"
        f"  Futures fees: `{f(agg['futures_fees'])}`\n"
        f"  Funding net: `{f(agg['funding_net'])}`\n"
        f"  Maker rewards: `{f(agg['maker_rewards'])}`\n\n"
        f"*Closing by account:*\n"
    )
    for label in SUBACCOUNTS:
        fc = fee_components[label]
        msg += f"  `{label}` ${closing[label]:,.2f}  fees net: {f(fc['total_net'])}\n"

    print("\n" + msg)

    # ── Send ──
    try:
        send_telegram(msg)
        print("Telegram sent.")
    except Exception as e:
        print(f"[ERROR] Telegram failed: {e}")

    # ── Save closing as next month's opening ──
    next_key = f"closing_{now.year}-{now.month:02d}"
    state[next_key] = total_closing
    save_state(state)
    print(f"State saved: {next_key} = ${total_closing:,.2f}")


if __name__ == "__main__":
    main()
