#!/usr/bin/env python3
"""
Daily subaccount funding check for ALL bot subaccounts.

Covers:
- CM1/CM2 (futures offset bot) — checks margin vs available USDT
- CMS1/CMS2 (spot wash trading bot) — checks liquid USDT buffer

Transfers from main account to any subaccount falling below its target.
Logs alert if main account balance is insufficient.

Usage:
  python3 scripts/daily_fund_check.py [--dry-run]
"""

import argparse
import hmac
import hashlib
import json
import logging
import sys
import time
from datetime import datetime

import requests

# Configuration
BASE_URL = "https://api.valr.com"
LOG_FILE = "/home/admin/.openclaw/workspace/bots/cm-bot-v2/logs/fund-subaccounts.log"

MAIN_API_KEY = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
MAIN_API_SECRET = "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"

# Subaccount definitions
# Futures offset bot (cm-bot-v2)
CM1_SUB = "1483472097578319872"
CM2_SUB = "1483472079069155328"
# Spot wash trading bot (cm-bot-spot, cm-bot-spot-illiquid)
CMS1_SUB = "1483815480334401536"
CMS2_SUB = "1483815498551132160"

LEVERAGE = 10
FUNDING_RATE_PER_DAY = 0.0003
DAYS_BUFFER = 7
MIN_TRANSFER = 5.0
MAIN_MIN_BUFFER = 20.0  # keep at least $20 in main

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def make_headers(method, path, body="", subaccount_id=""):
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method}{path}{body}"
    # For endpoints that require subaccount, include it in signature
    if subaccount_id and method != "POST":
        msg = f"{ts}{method}{path}{body}{subaccount_id}"
    sig = hmac.new(MAIN_API_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()
    headers = {
        "X-VALR-API-KEY": MAIN_API_KEY,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }
    if subaccount_id:
        headers["X-VALR-SUB-ACCOUNT-ID"] = subaccount_id
    return headers


def get_usdt_balance(subaccount_id=None):
    """Returns (available, reserved, total) USDT for an account."""
    path = "/v1/account/balances"
    headers = make_headers("GET", path, subaccount_id=subaccount_id or "")
    resp = requests.get(f"{BASE_URL}{path}", headers=headers, timeout=10)
    if resp.status_code != 200:
        return 0, 0, 0
    for b in resp.json():
        if b.get("currency") == "USDT":
            avail = float(b.get("available", 0) or 0)
            res = float(b.get("reserved", 0) or 0)
            return avail, res, avail + res
    return 0, 0, 0


def get_main_usdt():
    avail, res, total = get_usdt_balance()
    return avail


def get_open_positions(subaccount_id):
    path = "/v1/positions/open"
    headers = make_headers("GET", path, subaccount_id=subaccount_id)
    resp = requests.get(f"{BASE_URL}{path}", headers=headers, timeout=10)
    if resp.status_code == 200:
        return resp.json()
    return []


def get_market_price(pair):
    path = f"/v1/public/{pair}/marketsummary"
    headers = make_headers("GET", path)
    resp = requests.get(f"{BASE_URL}{path}", headers=headers, timeout=10)
    if resp.status_code == 200:
        d = resp.json()
        return float(d.get("lastTradedPrice", 0))
    return 0


def calc_futures_needed(subaccount_id, label):
    """Calculate USDT needed for a futures subaccount (1 week buffer)."""
    positions = get_open_positions(subaccount_id)
    if not positions:
        return 0

    total_notional = 0
    for pos in positions:
        pair = pos.get("pair", "")
        quantity = abs(float(pos.get("quantity", 0) or 0))
        price = get_market_price(pair)
        if price > 0 and quantity > 0:
            total_notional += quantity * price

    if total_notional == 0:
        return 0

    margin = total_notional / LEVERAGE
    funding_weekly = total_notional * FUNDING_RATE_PER_DAY * DAYS_BUFFER
    # Buffer for order placement: 5% of notional
    order_buffer = total_notional * 0.05

    total = margin + funding_weekly + order_buffer
    logger.info(f"  {label}: notional=${total_notional:.2f} → margin=${margin:.2f} + funding=${funding_weekly:.2f} + buffer=${order_buffer:.2f} = ${total:.2f}")
    return total


def calc_spot_needed(label, min_buffer=50.0):
    """Spot subaccounts need a minimum liquid USDT buffer for wash trading."""
    avail, res, total = get_usdt_balance(
        CMS1_SUB if label == "CMS1" else CMS2_SUB
    )
    # Need at least min_buffer USDT available (not locked in orders)
    needed = min_buffer
    logger.info(f"  {label}: avail=${avail:.2f} reserved=${res:.2f} total=${total:.2f} → target avail=${needed:.2f}")
    return needed


def transfer(from_id, to_id, amount, dry_run=False):
    path = "/v1/account/subaccounts/transfer"
    body = {
        "fromId": from_id,
        "toId": to_id,
        "currencyCode": "USDT",
        "amount": f"{amount:.2f}",
        "allowBorrow": False,
    }
    body_json = json.dumps(body)

    if dry_run:
        logger.info(f"  [DRY-RUN] Would transfer ${amount:.2f} from {from_id} → {to_id}")
        return True

    headers = make_headers("POST", path, body_json)
    resp = requests.post(f"{BASE_URL}{path}", headers=headers, json=body, timeout=10)

    if resp.status_code in [200, 202]:
        tid = resp.json().get("id", "?")
        logger.info(f"  ✅ Transfer ${amount:.2f} from {from_id} → {to_id} (id={tid})")
        return True
    else:
        logger.error(f"  ❌ Transfer failed: {resp.status_code} - {resp.text}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logger.info("=" * 70)
    logger.info(f"Daily Subaccount Funding Check - {datetime.now().isoformat()}")
    logger.info("=" * 70)

    main_balance = get_main_usdt()
    logger.info(f"Main account USDT: ${main_balance:.2f}")

    if main_balance < MAIN_MIN_BUFFER:
        logger.warning(f"⚠️  Main account too low (${main_balance:.2f}), cannot fund subaccounts")
        return

    # Calculate needs
    needs = {}

    # Futures subs
    cm1_needed = calc_futures_needed(CM1_SUB, "CM1")
    cm1_avail, cm1_res, cm1_total = get_usdt_balance(CM1_SUB)
    cm1_shortfall = max(0, cm1_needed - cm1_avail)
    needs[CM1_SUB] = ("CM1", cm1_avail, cm1_res, cm1_needed, cm1_shortfall)
    logger.info(f"  CM1: avail=${cm1_avail:.2f} reserved=${cm1_res:.2f} needed=${cm1_needed:.2f} shortfall=${cm1_shortfall:.2f}")

    cm2_needed = calc_futures_needed(CM2_SUB, "CM2")
    cm2_avail, cm2_res, cm2_total = get_usdt_balance(CM2_SUB)
    cm2_shortfall = max(0, cm2_needed - cm2_avail)
    needs[CM2_SUB] = ("CM2", cm2_avail, cm2_res, cm2_needed, cm2_shortfall)
    logger.info(f"  CM2: avail=${cm2_avail:.2f} reserved=${cm2_res:.2f} needed=${cm2_needed:.2f} shortfall=${cm2_shortfall:.2f}")

    # Spot subs
    cms1_needed = calc_spot_needed("CMS1")
    cms1_avail, cms1_res, cms1_total = get_usdt_balance(CMS1_SUB)
    cms1_shortfall = max(0, cms1_needed - cms1_avail)
    needs[CMS1_SUB] = ("CMS1", cms1_avail, cms1_res, cms1_needed, cms1_shortfall)
    logger.info(f"  CMS1: avail=${cms1_avail:.2f} reserved=${cms1_res:.2f} needed=${cms1_needed:.2f} shortfall=${cms1_shortfall:.2f}")

    cms2_needed = calc_spot_needed("CMS2")
    cms2_avail, cms2_res, cms2_total = get_usdt_balance(CMS2_SUB)
    cms2_shortfall = max(0, cms2_needed - cms2_avail)
    needs[CMS2_SUB] = ("CMS2", cms2_avail, cms2_res, cms2_needed, cms2_shortfall)
    logger.info(f"  CMS2: avail=${cms2_avail:.2f} reserved=${cms2_res:.2f} needed=${cms2_needed:.2f} shortfall=${cms2_shortfall:.2f}")

    total_shortfall = sum(n[4] for n in needs.values())

    if total_shortfall < MIN_TRANSFER:
        logger.info("✅ All subaccounts adequately funded, no action needed")
        return

    logger.info(f"Total shortfall: ${total_shortfall:.2f}")
    available = main_balance - MAIN_MIN_BUFFER

    if available < MIN_TRANSFER:
        logger.warning(f"⚠️  Main account insufficient (${available:.2f} available after buffer)")
        return

    if available >= total_shortfall:
        # Fund all shortfalls fully
        for sub_id, (label, avail, res, needed, shortfall) in needs.items():
            if shortfall >= MIN_TRANSFER:
                logger.info(f"  Transferring ${shortfall:.2f} to {label}...")
                transfer(0, int(sub_id), shortfall, dry_run=args.dry_run)
                time.sleep(0.3)
    else:
        # Partial funding — proportional
        logger.warning(f"⚠️  Partial funding: ${available:.2f} available vs ${total_shortfall:.2f} needed")
        for sub_id, (label, avail, res, needed, shortfall) in needs.items():
            if shortfall > 0:
                proportion = shortfall / total_shortfall
                amount = min(shortfall, available * proportion)
                if amount >= MIN_TRANSFER:
                    logger.info(f"  Transferring ${amount:.2f} to {label} ({proportion*100:.0f}% of shortfall)...")
                    transfer(0, int(sub_id), amount, dry_run=args.dry_run)
                    time.sleep(0.3)

        remaining = total_shortfall - available
        if remaining > 0:
            logger.warning(f"⚠️  Unfunded shortfall: ${remaining:.2f}")

    # Final summary
    time.sleep(1)
    for sub_id, (label, _, _, _, _) in needs.items():
        avail, res, total = get_usdt_balance(sub_id)
        logger.info(f"  {label}: avail=${avail:.2f} reserved=${res:.2f} total=${total:.2f}")

    main_final = get_main_usdt()
    logger.info(f"Main: ${main_final:.2f}")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
