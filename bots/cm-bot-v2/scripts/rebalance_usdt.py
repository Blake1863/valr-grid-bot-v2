#!/usr/bin/env python3
"""
Rebalance USDT balances between CM1 and CM2 subaccounts.
Runs every 10 minutes via cron (alongside position netting).

Transfers USDT from the account with more to the account with less,
aiming for a 50/50 split.
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
LOG_FILE = "/home/admin/.openclaw/workspace/bots/cm-bot-v2/logs/usdt-rebalance.log"

# API Credentials
MAIN_API_KEY = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
MAIN_API_SECRET = "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"
CM1_SUBACCOUNT_ID = "1483472097578319872"
CM2_SUBACCOUNT_ID = "1483472079069155328"

# Rebalancing thresholds
MIN_TRANSFER_AMOUNT = 5.0  # Minimum USDT to transfer (avoid tiny transfers)
REBALANCE_THRESHOLD_PCT = 0.60  # Rebalance if one account has >60% of total

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def make_headers(api_key, api_secret, method, path, body="", subaccount_id=""):
    """Generate VALR API authentication headers."""
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method}{path}{body}{subaccount_id}"
    sig = hmac.new(api_secret.encode(), msg.encode(), hashlib.sha512).hexdigest()
    headers = {
        "X-VALR-API-KEY": api_key,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }
    if subaccount_id:
        headers["X-VALR-SUB-ACCOUNT-ID"] = subaccount_id
    return headers


def get_balances(api_key, api_secret, subaccount_id=""):
    """Fetch account balances."""
    path = "/v1/account/balances"
    headers = make_headers(api_key, api_secret, "GET", path, "", subaccount_id)
    url = f"{BASE_URL}{path}"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching balances: {e}")
    return []


def transfer_usdt(api_key, api_secret, from_subaccount, to_subaccount, amount, dry_run=False):
    """Transfer USDT between subaccounts."""
    path = "/v1/account/subaccounts/transfer"
    
    body = {
        "fromId": from_subaccount,
        "toId": to_subaccount,
        "currencyCode": "USDT",
        "amount": f"{amount:.2f}",
        "allowBorrow": False,
    }
    
    body_json = json.dumps(body)
    headers = make_headers(api_key, api_secret, "POST", path, body_json, "")  # No subaccount for transfer endpoint
    url = f"{BASE_URL}{path}"
    
    if dry_run:
        logger.info(f"[DRY-RUN] Would transfer ${amount:.2f} USDT from {from_subaccount} to {to_subaccount}")
        return {"status": "dry-run"}
    
    try:
        response = requests.post(url, headers=headers, json=body, timeout=10)
        result = response.json()
        
        if response.status_code in [200, 202]:
            logger.info(f"✅ Transfer successful: ${amount:.2f} USDT from {from_subaccount} to {to_subaccount}")
            return {"status": "success", "amount": amount}
        else:
            logger.error(f"❌ Transfer failed: {response.status_code} - {result}")
            return {"status": "failed", "error": result}
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Error during transfer: {e}")
        return {"status": "error", "error": str(e)}


def rebalance_usdt(dry_run=False):
    """Check and rebalance USDT between CM1 and CM2."""
    logger.info("=== USDT Balance Rebalance ===")
    
    # Fetch balances
    cm1_balances = get_balances(MAIN_API_KEY, MAIN_API_SECRET, CM1_SUBACCOUNT_ID)
    cm2_balances = get_balances(MAIN_API_KEY, MAIN_API_SECRET, CM2_SUBACCOUNT_ID)
    
    # Find USDT balances
    cm1_usdt = next((b for b in cm1_balances if b.get("currency") == "USDT"), {})
    cm2_usdt = next((b for b in cm2_balances if b.get("currency") == "USDT"), {})
    
    cm1_available = float(cm1_usdt.get("available", 0) or 0)
    cm2_available = float(cm2_usdt.get("available", 0) or 0)
    
    total = cm1_available + cm2_available
    
    logger.info(f"CM1 USDT: ${cm1_available:.2f}")
    logger.info(f"CM2 USDT: ${cm2_available:.2f}")
    logger.info(f"Total USDT: ${total:.2f}")
    
    if total < 1.0:
        logger.info("Total USDT too low to rebalance")
        return
    
    # Calculate percentages
    cm1_pct = cm1_available / total if total > 0 else 0
    cm2_pct = cm2_available / total if total > 0 else 0
    
    logger.info(f"CM1: {cm1_pct*100:.1f}% | CM2: {cm2_pct*100:.1f}%")
    
    # Check if rebalancing needed
    if cm1_pct <= REBALANCE_THRESHOLD_PCT and cm2_pct <= REBALANCE_THRESHOLD_PCT:
        logger.info(f"✅ Balances within threshold (both ≤{REBALANCE_THRESHOLD_PCT*100:.0f}%)")
        return
    
    # Calculate target (50/50 split)
    target = total / 2
    logger.info(f"Target per account: ${target:.2f}")
    
    # Determine transfer direction and amount
    if cm1_available > cm2_available:
        from_account = CM1_SUBACCOUNT_ID
        to_account = CM2_SUBACCOUNT_ID
        from_label = "CM1"
        to_label = "CM2"
        surplus = cm1_available - target
    else:
        from_account = CM2_SUBACCOUNT_ID
        to_account = CM1_SUBACCOUNT_ID
        from_label = "CM2"
        to_label = "CM1"
        surplus = cm2_available - target
    
    # Round down to avoid transferring too much
    transfer_amount = min(surplus, surplus * 0.95)  # Transfer 95% of surplus
    
    if transfer_amount < MIN_TRANSFER_AMOUNT:
        logger.info(f"Transfer amount ${transfer_amount:.2f} < minimum ${MIN_TRANSFER_AMOUNT}, skipping")
        return
    
    logger.info(f"⚠️  Imbalance detected: {from_label} has ${surplus:.2f} surplus")
    logger.info(f"Transferring ${transfer_amount:.2f} USDT from {from_label} to {to_label}")
    
    # Execute transfer
    result = transfer_usdt(
        MAIN_API_KEY, 
        MAIN_API_SECRET, 
        from_account, 
        to_account, 
        transfer_amount,
        dry_run=dry_run
    )
    
    if result.get("status") == "success":
        logger.info(f"✅ Rebalance complete: ${transfer_amount:.2f} USDT {from_label} → {to_label}")
    elif result.get("status") == "dry-run":
        logger.info("✅ Dry run complete (no actual transfer)")
    else:
        logger.error(f"❌ Rebalance failed: {result}")


def main():
    parser = argparse.ArgumentParser(description="Rebalance USDT between CM1 and CM2")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    args = parser.parse_args()
    
    logger.info("=" * 60)
    logger.info(f"Starting USDT rebalance (dry_run={args.dry_run})")
    logger.info(f"Timestamp: {datetime.now().isoformat()}")
    logger.info("=" * 60)
    
    rebalance_usdt(dry_run=args.dry_run)
    
    logger.info("=" * 60)
    logger.info("USDT rebalance complete")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
