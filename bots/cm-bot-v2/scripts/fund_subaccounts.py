#!/usr/bin/env python3
"""
Transfer USDT from main account to CM1 and CM2 subaccounts.
Used to fund the subaccounts for position offsetting.
"""

import hmac
import hashlib
import json
import logging
import sys
import time
import requests

BASE_URL = "https://api.valr.com"
LOG_FILE = "/home/admin/.openclaw/workspace/bots/cm-bot-v2/logs/fund-subaccounts.log"

MAIN_API_KEY = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
MAIN_API_SECRET = "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"
CM1_SUBACCOUNT_ID = "1483472097578319872"
CM2_SUBACCOUNT_ID = "1483472079069155328"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def make_headers(method, path, body=""):
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method}{path}{body}"
    sig = hmac.new(MAIN_API_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()
    return {
        "X-VALR-API-KEY": MAIN_API_KEY,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }


def get_main_balance():
    """Get main account USDT balance."""
    path = "/v1/account/balances"
    headers = make_headers("GET", path)
    resp = requests.get(f"{BASE_URL}{path}", headers=headers, timeout=10)
    if resp.status_code == 200:
        for b in resp.json():
            if b.get("currency") == "USDT":
                return float(b.get("available", 0) or 0)
    return 0


def transfer_to_subaccount(subaccount_id, amount):
    """Transfer USDT from main to subaccount."""
    path = "/v1/account/subaccounts/transfer"
    body = {
        "fromId": 0,  # Main account
        "toId": subaccount_id,
        "currencyCode": "USDT",
        "amount": f"{amount:.2f}",
        "allowBorrow": False,
    }
    body_json = json.dumps(body)
    headers = make_headers("POST", path, body_json)
    resp = requests.post(f"{BASE_URL}{path}", headers=headers, json=body, timeout=10)
    
    if resp.status_code in [200, 202]:
        logger.info(f"✅ Transferred ${amount:.2f} USDT to {subaccount_id}")
        return True
    else:
        logger.error(f"❌ Transfer failed: {resp.status_code} - {resp.text}")
        return False


def main():
    logger.info("=" * 60)
    logger.info("Funding subaccounts from main account")
    logger.info("=" * 60)
    
    main_balance = get_main_balance()
    logger.info(f"Main account USDT balance: ${main_balance:.2f}")
    
    if main_balance < 10:
        logger.warning(f"Main balance too low (${main_balance:.2f}), please fund main account first")
        return
    
    # Transfer equal amounts to CM1 and CM2
    # Keep $10 buffer in main account
    amount_to_transfer = max(0, main_balance - 10)
    amount_per_subaccount = amount_to_transfer / 2
    
    if amount_per_subaccount < 5:
        logger.warning("Not enough to fund subaccounts (need at least $5 each)")
        return
    
    logger.info(f"Transferring ${amount_per_subaccount:.2f} to each subaccount...")
    
    success1 = transfer_to_subaccount(CM1_SUBACCOUNT_ID, amount_per_subaccount)
    time.sleep(0.5)
    success2 = transfer_to_subaccount(CM2_SUBACCOUNT_ID, amount_per_subaccount)
    
    if success1 and success2:
        logger.info(f"✅ Successfully funded both subaccounts with ${amount_per_subaccount:.2f} each")
    else:
        logger.error("❌ One or both transfers failed")
    
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
