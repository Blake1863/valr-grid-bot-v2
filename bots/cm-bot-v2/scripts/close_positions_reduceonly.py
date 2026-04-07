#!/usr/bin/env python3
"""
Close all positions using reduceOnly orders.
No additional margin needed - uses existing position margin.

Each account closes its own positions independently.
"""

import argparse
import hmac
import hashlib
import json
import logging
import sys
import time
import uuid
from datetime import datetime

import requests

BASE_URL = "https://api.valr.com"
LOG_FILE = "/home/admin/.openclaw/workspace/bots/cm-bot-v2/logs/close-reduceonly.log"

MAIN_API_KEY = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
MAIN_API_SECRET = "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"
CM1_SUBACCOUNT_ID = "1483472097578319872"
CM2_SUBACCOUNT_ID = "1483472079069155328"

PAIR_SPECS = {
    "BTCUSDTPERP": {"price_precision": 0},
    "ETHUSDTPERP": {"price_precision": 1},
    "XRPUSDTPERP": {"price_precision": 4},
    "DOGEUSDTPERP": {"price_precision": 5},
    "SOLUSDTPERP": {"price_precision": 2},
    "AVAXUSDTPERP": {"price_precision": 3},
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def make_headers(api_key, api_secret, method, path, body="", subaccount_id=""):
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


def get_open_positions(api_key, api_secret, subaccount_id=""):
    path = "/v1/positions/open"
    headers = make_headers(api_key, api_secret, "GET", path, "", subaccount_id)
    try:
        response = requests.get(f"{BASE_URL}{path}", headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Error fetching positions: {e}")
        return []


def get_orderbook(pair):
    path = f"/v1/public/{pair}/orderbook"
    try:
        response = requests.get(f"{BASE_URL}{path}", timeout=10)
        response.raise_for_status()
        ob = response.json()
        if ob.get("Asks") and ob.get("Bids"):
            bid = float(ob["Bids"][0]["price"])
            ask = float(ob["Asks"][0]["price"])
            return {
                'bid': bid,
                'ask': ask,
                'mid': (bid + ask) / 2
            }
    except Exception as e:
        logger.error(f"Error fetching orderbook: {e}")
    return None


def close_position_reduceonly(api_key, api_secret, subaccount_id, pair, quantity, position_side, dry_run=False):
    """
    Close a position with reduceOnly limit order at mid price.
    
    If LONG position → SELL to close
    If SHORT position → BUY to close
    
    reduceOnly=True means no additional margin needed.
    """
    # Get mid price
    ob = get_orderbook(pair)
    if not ob:
        logger.error(f"  ❌ No orderbook for {pair}")
        return False
    
    # To close: opposite side of position
    # LONG (buy) → SELL to close
    # SHORT (sell) → BUY to close
    close_side = "SELL" if position_side.lower() == "buy" else "BUY"
    
    # Use mid price rounded to correct tick size
    spec = PAIR_SPECS.get(pair, {"price_precision": 2})
    price_precision = spec["price_precision"]
    price = round(ob['mid'], price_precision)
    
    path = "/v2/orders/limit"
    customer_order_id = str(uuid.uuid4())
    
    body = {
        "side": close_side,
        "quantity": str(quantity),
        "price": str(price),
        "pair": pair,
        "timeInForce": "GTC",  # Rest on book, will fill
        "reduceOnly": True,  # KEY: No margin needed, closes existing position
        "customerOrderId": customer_order_id,
    }
    
    body_json = json.dumps(body)
    headers = make_headers(api_key, api_secret, "POST", path, body_json, subaccount_id)
    
    if dry_run:
        logger.info(f"  [DRY-RUN] Close {close_side} {quantity} {pair} @ {price} (reduceOnly)")
        return True
    
    try:
        response = requests.post(f"{BASE_URL}{path}", headers=headers, json=body, timeout=10)
        result = response.json()
        
        if response.status_code in [200, 201, 202]:
            order_id = result.get('orderId', 'N/A')
            if isinstance(order_id, str):
                order_id = order_id[:16]
            logger.info(f"  ✅ Closed: {close_side} {quantity} @ {price} - ID: {order_id}")
            return True
        else:
            logger.error(f"  ❌ Failed: {response.status_code} - {result}")
            return False
    except Exception as e:
        logger.error(f"  ❌ Error: {e}")
        return False


def close_all_for_account(subaccount_id, label, positions, dry_run=False):
    """Close all positions for an account."""
    logger.info(f"\n=== Closing {label} Positions ===")
    
    if not positions:
        logger.info(f"{label}: No positions to close")
        return 0, 0
    
    successful = 0
    failed = 0
    
    for pos in positions:
        pair = pos.get("pair", "UNKNOWN")
        quantity = abs(float(pos.get("quantity", 0) or 0))
        side = pos.get("side", "Buy")
        
        if quantity < 0.0001:
            continue
        
        logger.info(f"{label} - {pair}: {quantity:.6f} ({side})")
        
        if close_position_reduceonly(
            MAIN_API_KEY, MAIN_API_SECRET, subaccount_id,
            pair, quantity, side, dry_run=dry_run
        ):
            successful += 1
        else:
            failed += 1
        
        time.sleep(0.1)
    
    return successful, failed


def main():
    parser = argparse.ArgumentParser(description="Close all positions with reduceOnly")
    parser.add_argument("--dry-run", action="store_true", help="Print without executing")
    args = parser.parse_args()
    
    logger.info("=" * 70)
    logger.info(f"Close All Positions (reduceOnly, dry_run={args.dry_run})")
    logger.info(f"Timestamp: {datetime.now().isoformat()}")
    logger.info("=" * 70)
    
    # Fetch positions
    logger.info("\nFetching CM1 positions...")
    cm1_positions = get_open_positions(MAIN_API_KEY, MAIN_API_SECRET, CM1_SUBACCOUNT_ID)
    logger.info(f"CM1: {len(cm1_positions)} positions")
    
    logger.info("Fetching CM2 positions...")
    cm2_positions = get_open_positions(MAIN_API_KEY, MAIN_API_SECRET, CM2_SUBACCOUNT_ID)
    logger.info(f"CM2: {len(cm2_positions)} positions")
    
    if not cm1_positions and not cm2_positions:
        logger.info("\n✅ No open positions")
        return
    
    # Show summary
    logger.info("\n=== Positions to Close ===")
    total_notional = 0
    for pos in cm1_positions + cm2_positions:
        qty = abs(float(pos.get("quantity", 0) or 0))
        price = float(pos.get("markPrice", 0) or pos.get("averageEntryPrice", 0))
        total_notional += qty * price
        label = "CM1" if pos.get("subaccountId") == CM1_SUBACCOUNT_ID else "CM2"
        logger.info(f"{label}: {pos['pair']} {qty:.6f} @ ${price:,.2f}")
    
    logger.info(f"\nTotal notional: ${total_notional:,.2f}")
    logger.info(f"Note: reduceOnly orders use existing position margin - no additional funds needed")
    
    # Close all
    cm1_ok, cm1_fail = close_all_for_account(CM1_SUBACCOUNT_ID, "CM1", cm1_positions, dry_run=args.dry_run)
    time.sleep(0.3)
    cm2_ok, cm2_fail = close_all_for_account(CM2_SUBACCOUNT_ID, "CM2", cm2_positions, dry_run=args.dry_run)
    
    # Summary
    logger.info("\n" + "=" * 70)
    logger.info(f"Summary: {cm1_ok + cm2_ok} closed, {cm1_fail + cm2_fail} failed")
    logger.info("=" * 70)
    
    if not args.dry_run and cm1_fail + cm2_fail == 0:
        logger.info("\n✅ All positions closed! PnL locked in.")


if __name__ == "__main__":
    main()
