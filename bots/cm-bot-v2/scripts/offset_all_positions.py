#!/usr/bin/env python3
"""
Offset ALL positions between CM1 and CM2 subaccounts.
Closes all open positions by having accounts trade against each other.
Locks in PnL for all positions.

Usage:
  python3 offset_all_positions.py --dry-run   # Show what would be done
  python3 offset_all_positions.py             # Actually execute
"""

import argparse
import hmac
import hashlib
import json
import logging
import math
import sys
import time
import uuid
from datetime import datetime

import requests

# Configuration
BASE_URL = "https://api.valr.com"
LOG_FILE = "/home/admin/.openclaw/workspace/bots/cm-bot-v2/logs/offset-positions.log"

# API Credentials
MAIN_API_KEY = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
MAIN_API_SECRET = "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"
CM1_SUBACCOUNT_ID = "1483472097578319872"
CM2_SUBACCOUNT_ID = "1483472079069155328"

# Pair specifications
PAIR_SPECS = {
    "BTCUSDTPERP": {"min_qty": 0.0001, "qty_precision": 4, "price_precision": 0},
    "ETHUSDTPERP": {"min_qty": 0.001, "qty_precision": 3, "price_precision": 1},
    "XRPUSDTPERP": {"min_qty": 2, "qty_precision": 0, "price_precision": 4},
    "DOGEUSDTPERP": {"min_qty": 6, "qty_precision": 0, "price_precision": 5},
    "SOLUSDTPERP": {"min_qty": 0.01, "qty_precision": 2, "price_precision": 2},
    "AVAXUSDTPERP": {"min_qty": 0.03, "qty_precision": 2, "price_precision": 3},
}

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


def get_open_positions(api_key, api_secret, subaccount_id=""):
    """Fetch open positions for an account."""
    path = "/v1/positions/open"
    headers = make_headers(api_key, api_secret, "GET", path, "", subaccount_id)
    url = f"{BASE_URL}{path}"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching positions: {e}")
        return []


def get_orderbook(pair):
    """Fetch orderbook for a pair."""
    path = f"/v1/public/{pair}/orderbook"
    url = f"{BASE_URL}{path}"
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching orderbook for {pair}: {e}")
        return None


def place_limit_order(api_key, api_secret, subaccount_id, pair, side, quantity, price, 
                     time_in_force="GTC", post_only=False, dry_run=False):
    """Place a limit order."""
    path = "/v2/orders/limit"
    customer_order_id = str(uuid.uuid4())
    
    body = {
        "side": side,
        "quantity": str(quantity),
        "price": str(price),
        "pair": pair,
        "postOnly": post_only,
        "timeInForce": time_in_force,
        "customerOrderId": customer_order_id,
    }
    
    body_json = json.dumps(body)
    headers = make_headers(api_key, api_secret, "POST", path, body_json, subaccount_id)
    url = f"{BASE_URL}{path}"
    
    if dry_run:
        logger.info(f"  [DRY-RUN] {side} {quantity} {pair} @ {price} (TOF={time_in_force})")
        return {"status": "dry-run"}
    
    try:
        response = requests.post(url, headers=headers, json=body, timeout=10)
        result = response.json()
        
        if response.status_code in [200, 201, 202]:
            logger.info(f"  ✅ Order placed: {side} {quantity} {pair} @ {price} - ID: {result.get('orderId', 'N/A')[:16]}")
            return {"status": "success", "orderId": result.get('orderId')}
        else:
            logger.error(f"  ❌ Order failed: {response.status_code} - {result}")
            return {"status": "failed", "error": result}
            
    except requests.exceptions.RequestException as e:
        logger.error(f"  ❌ Error placing order: {e}")
        return {"status": "error", "error": str(e)}


def offset_position(pair, qty_to_offset, price, cm1_is_longer, dry_run=False):
    """
    Offset position between CM1 and CM2.
    
    If CM1 is longer (has more long position):
    - CM1 SELLs qty_to_offset (closes long)
    - CM2 BUYs qty_to_offset (opens long to match)
    
    If CM2 is longer:
    - CM2 SELLs qty_to_offset
    - CM1 BUYs qty_to_offset
    """
    spec = PAIR_SPECS.get(pair, {"qty_precision": 2, "price_precision": 2, "min_qty": 0.01})
    
    # Round to precision
    trade_qty = round(qty_to_offset, spec["qty_precision"])
    trade_qty = max(trade_qty, spec["min_qty"])
    
    formatted_qty = f"{trade_qty:.{spec['qty_precision']}f}"
    formatted_price = f"{price:.{spec['price_precision']}f}"
    
    logger.info(f"{pair}: Offsetting {formatted_qty} @ {formatted_price}")
    
    if cm1_is_longer:
        # CM1 sells (closes long), CM2 buys (opens long)
        logger.info(f"  CM1 SELLs {formatted_qty} (close long)")
        logger.info(f"  CM2 BUYs {formatted_qty} (open long)")
        
        result1 = place_limit_order(
            MAIN_API_KEY, MAIN_API_SECRET, CM1_SUBACCOUNT_ID,
            pair, "SELL", formatted_qty, formatted_price,
            time_in_force="GTC", post_only=False, dry_run=dry_run
        )
        
        time.sleep(1.0)  # 1 second delay to ensure maker is on book before taker hits
        
        result2 = place_limit_order(
            MAIN_API_KEY, MAIN_API_SECRET, CM2_SUBACCOUNT_ID,
            pair, "BUY", formatted_qty, formatted_price,
            time_in_force="IOC", post_only=False, dry_run=dry_run
        )
    else:
        # CM2 sells (closes long), CM1 buys (opens long)
        logger.info(f"  CM2 SELLs {formatted_qty} (close long)")
        logger.info(f"  CM1 BUYs {formatted_qty} (open long)")
        
        result1 = place_limit_order(
            MAIN_API_KEY, MAIN_API_SECRET, CM2_SUBACCOUNT_ID,
            pair, "SELL", formatted_qty, formatted_price,
            time_in_force="GTC", post_only=False, dry_run=dry_run
        )
        
        time.sleep(0.01)
        
        result2 = place_limit_order(
            MAIN_API_KEY, MAIN_API_SECRET, CM1_SUBACCOUNT_ID,
            pair, "BUY", formatted_qty, formatted_price,
            time_in_force="IOC", post_only=False, dry_run=dry_run
        )
    
    return result1.get("status") == "success" and result2.get("status") == "success"


def offset_all_positions(cm1_positions, cm2_positions, dry_run=False):
    """Offset ALL positions between CM1 and CM2."""
    logger.info("=== Offsetting All Positions ===")
    
    # Build position maps
    cm1_map = {pos["pair"]: pos for pos in cm1_positions}
    cm2_map = {pos["pair"]: pos for pos in cm2_positions}
    
    # Get all unique pairs
    all_pairs = set(cm1_map.keys()) | set(cm2_map.keys())
    
    successful_offsets = 0
    total_offsets = 0
    
    for pair in all_pairs:
        if pair not in PAIR_SPECS:
            logger.info(f"{pair}: Skipping (not in pair specs)")
            continue
        
        cm1_pos = cm1_map.get(pair, {})
        cm2_pos = cm2_map.get(pair, {})
        
        # Get quantities (positive for long, negative for short)
        cm1_qty = float(cm1_pos.get("quantity", 0) or 0)
        cm2_qty = float(cm2_pos.get("quantity", 0) or 0)
        
        # Skip if both have no position
        if abs(cm1_qty) < 0.0001 and abs(cm2_qty) < 0.0001:
            logger.info(f"{pair}: No positions to offset")
            continue
        
        # Get mid price
        orderbook = get_orderbook(pair)
        if not orderbook or not orderbook.get("Asks"):
            logger.warning(f"{pair}: No orderbook data, skipping")
            continue
        
        ask_price = float(orderbook["Asks"][0]["price"])
        bid_price = float(orderbook["Bids"][0]["price"]) if orderbook.get("Bids") else ask_price
        mid_price = (ask_price + bid_price) / 2
        
        # Calculate notional for each position
        cm1_notional = abs(cm1_qty) * mid_price
        cm2_notional = abs(cm2_qty) * mid_price
        
        logger.info(f"\n{pair}:")
        logger.info(f"  CM1: {cm1_qty:+.6f} (${cm1_notional:.2f})")
        logger.info(f"  CM2: {cm2_qty:+.6f} (${cm2_notional:.2f})")
        
        # Determine offset amount (smaller of the two positions)
        # We offset the smaller position first to close it completely
        offset_qty = min(abs(cm1_qty), abs(cm2_qty))
        
        if offset_qty < PAIR_SPECS[pair]["min_qty"]:
            logger.info(f"  Offset amount too small, skipping")
            continue
        
        # Determine which account is longer (has more long position)
        # For simplicity: offset the smaller position
        cm1_is_longer = abs(cm1_qty) >= abs(cm2_qty)
        
        total_offsets += 1
        
        if offset_position(pair, offset_qty, mid_price, cm1_is_longer, dry_run=dry_run):
            successful_offsets += 1
            logger.info(f"  ✅ Offset successful")
        else:
            logger.error(f"  ❌ Offset failed")
    
    logger.info(f"\n=== Summary ===")
    logger.info(f"Total offsets attempted: {total_offsets}")
    logger.info(f"Successful: {successful_offsets}")
    logger.info(f"Failed: {total_offsets - successful_offsets}")


def main():
    parser = argparse.ArgumentParser(description="Offset ALL positions between CM1 and CM2")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    parser.add_argument("--confirm", action="store_true", help="Require confirmation before executing")
    args = parser.parse_args()
    
    logger.info("=" * 70)
    logger.info(f"Starting position offset (dry_run={args.dry_run})")
    logger.info(f"Timestamp: {datetime.now().isoformat()}")
    logger.info("=" * 70)
    
    # Confirm if not dry run
    if not args.dry_run and args.confirm:
        print("\n⚠️  WARNING: This will close ALL positions between CM1 and CM2")
        print("    This action locks in PnL for all offset positions.")
        response = input("\nAre you sure you want to proceed? Type 'YES' to confirm: ")
        if response != "YES":
            logger.info("Aborted by user")
            return
    
    # Fetch positions
    logger.info("\nFetching CM1 positions...")
    cm1_positions = get_open_positions(MAIN_API_KEY, MAIN_API_SECRET, CM1_SUBACCOUNT_ID)
    logger.info(f"CM1 has {len(cm1_positions)} open positions")
    
    logger.info("Fetching CM2 positions...")
    cm2_positions = get_open_positions(MAIN_API_KEY, MAIN_API_SECRET, CM2_SUBACCOUNT_ID)
    logger.info(f"CM2 has {len(cm2_positions)} open positions")
    
    if not cm1_positions and not cm2_positions:
        logger.info("\nNo open positions to offset")
        return
    
    # Show positions summary
    logger.info("\n=== Current Positions ===")
    for pos in cm1_positions:
        qty = float(pos.get("quantity", 0) or 0)
        logger.info(f"CM1: {pos['pair']} {qty:+.6f} @ ${float(pos.get('markPrice', 0)):,.2f}")
    
    for pos in cm2_positions:
        qty = float(pos.get("quantity", 0) or 0)
        logger.info(f"CM2: {pos['pair']} {qty:+.6f} @ ${float(pos.get('markPrice', 0)):,.2f}")
    
    # Offset all positions
    offset_all_positions(cm1_positions, cm2_positions, dry_run=args.dry_run)
    
    logger.info("\n" + "=" * 70)
    logger.info("Position offset complete")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
