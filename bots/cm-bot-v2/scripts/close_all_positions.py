#!/usr/bin/env python3
"""
Close ALL open positions on CM1 and CM2.
Simply closes each position to market - no internal matching needed.
Locks in PnL for all positions.

Usage:
  python3 close_all_positions.py --dry-run   # Show what would be closed
  python3 close_all_positions.py             # Actually close all
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

# Configuration
BASE_URL = "https://api.valr.com"
LOG_FILE = "/home/admin/.openclaw/workspace/bots/cm-bot-v2/logs/close-all-positions.log"

# API Credentials
MAIN_API_KEY = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
MAIN_API_SECRET = "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"
CM1_SUBACCOUNT_ID = "1483472097578319872"
CM2_SUBACCOUNT_ID = "1483472079069155328"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
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


def get_mid_price(pair):
    """Get mid market price for a pair."""
    path = f"/v1/public/{pair}/orderbook"
    url = f"{BASE_URL}{path}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            ob = response.json()
            if ob.get("Asks") and ob.get("Bids"):
                ask = float(ob["Asks"][0]["price"])
                bid = float(ob["Bids"][0]["price"])
                return (ask + bid) / 2
    except:
        pass
    return None


def close_position(api_key, api_secret, subaccount_id, pair, quantity, side, dry_run=False):
    """
    Close a position by placing an opposite limit order at market price.
    
    If long (buy position), sell to close.
    If short (sell position), buy to close.
    """
    # Get current market price
    price = get_mid_price(pair)
    if not price:
        logger.error(f"  ❌ Could not get price for {pair}")
        return {"status": "failed", "error": "No price"}
    
    path = "/v2/orders/limit"
    customer_order_id = str(uuid.uuid4())
    
    # To close: opposite side of current position
    close_side = "SELL" if side.lower() == "buy" else "BUY"
    
    # Cross the spread to ensure immediate fill
    # If selling, use bid price (will match immediately)
    # If buying, use ask price (will match immediately)
    if close_side == "SELL":
        fill_price = price * 0.999  # Slightly below mid to hit bids
    else:
        fill_price = price * 1.001  # Slightly above mid to hit asks
    
    body = {
        "side": close_side,
        "quantity": str(quantity),
        "price": str(fill_price),
        "pair": pair,
        "timeInForce": "IOC",
        "customerOrderId": customer_order_id,
    }
    
    body_json = json.dumps(body)
    headers = make_headers(api_key, api_secret, "POST", path, body_json, subaccount_id)
    url = f"{BASE_URL}{path}"
    
    if dry_run:
        logger.info(f"  [DRY-RUN] Close {close_side} {quantity} {pair}")
        return {"status": "dry-run"}
    
    try:
        response = requests.post(url, headers=headers, json=body, timeout=10)
        result = response.json()
        
        if response.status_code in [200, 201, 202]:
            order_id = result.get('orderId', 'N/A')
            if isinstance(order_id, str):
                order_id = order_id[:16]
            logger.info(f"  ✅ Close order placed: {close_side} {quantity} {pair} - ID: {order_id}")
            return {"status": "success", "orderId": result.get('orderId')}
        else:
            logger.error(f"  ❌ Close failed: {response.status_code} - {result}")
            return {"status": "failed", "error": result}
            
    except requests.exceptions.RequestException as e:
        logger.error(f"  ❌ Error closing position: {e}")
        return {"status": "error", "error": str(e)}


def close_all_positions(subaccount_id, label, positions, dry_run=False):
    """Close all positions for an account."""
    logger.info(f"\n=== Closing {label} Positions ===")
    
    if not positions:
        logger.info(f"{label}: No open positions")
        return 0, 0
    
    successful = 0
    failed = 0
    
    for pos in positions:
        pair = pos.get("pair", "UNKNOWN")
        quantity = float(pos.get("quantity", 0) or 0)
        side = pos.get("side", "Buy")  # Buy = long, Sell = short
        
        if abs(quantity) < 0.0001:
            logger.info(f"{pair}: Position too small, skipping")
            continue
        
        logger.info(f"{label} - {pair}: {quantity:+.6f} ({side})")
        
        result = close_position(
            MAIN_API_KEY, MAIN_API_SECRET, subaccount_id,
            pair, abs(quantity), side, dry_run=dry_run
        )
        
        if result.get("status") in ["success", "dry-run"]:
            successful += 1
        else:
            failed += 1
        
        time.sleep(0.1)  # Small delay between orders
    
    return successful, failed


def main():
    parser = argparse.ArgumentParser(description="Close ALL positions on CM1 and CM2")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    parser.add_argument("--confirm", action="store_true", help="Require confirmation before executing")
    args = parser.parse_args()
    
    logger.info("=" * 70)
    logger.info(f"Closing ALL positions (dry_run={args.dry_run})")
    logger.info(f"Timestamp: {datetime.now().isoformat()}")
    logger.info("=" * 70)
    
    # Confirm if not dry run
    if not args.dry_run and args.confirm:
        print("\n⚠️  WARNING: This will CLOSE ALL POSITIONS on CM1 and CM2")
        print("    This action locks in PnL for all positions.")
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
        logger.info("\n✅ No open positions to close")
        return
    
    # Show positions summary
    logger.info("\n=== Current Positions ===")
    total_notional = 0
    for pos in cm1_positions:
        qty = float(pos.get("quantity", 0) or 0)
        price = float(pos.get("markPrice", 0) or pos.get("averageEntryPrice", 0))
        notional = abs(qty) * price
        total_notional += notional
        logger.info(f"CM1: {pos['pair']} {qty:+.6f} @ ${price:,.2f} (${notional:.2f})")
    
    for pos in cm2_positions:
        qty = float(pos.get("quantity", 0) or 0)
        price = float(pos.get("markPrice", 0) or pos.get("averageEntryPrice", 0))
        notional = abs(qty) * price
        total_notional += notional
        logger.info(f"CM2: {pos['pair']} {qty:+.6f} @ ${price:,.2f} (${notional:.2f})")
    
    logger.info(f"\nTotal notional exposure: ${total_notional:,.2f}")
    
    # Close all positions
    cm1_success, cm1_failed = close_all_positions(CM1_SUBACCOUNT_ID, "CM1", cm1_positions, dry_run=args.dry_run)
    time.sleep(0.5)
    cm2_success, cm2_failed = close_all_positions(CM2_SUBACCOUNT_ID, "CM2", cm2_positions, dry_run=args.dry_run)
    
    # Summary
    logger.info("\n" + "=" * 70)
    logger.info("=== Summary ===")
    logger.info(f"CM1: {cm1_success} closed, {cm1_failed} failed")
    logger.info(f"CM2: {cm2_success} closed, {cm2_failed} failed")
    logger.info(f"Total: {cm1_success + cm2_success} closed, {cm1_failed + cm2_failed} failed")
    logger.info("=" * 70)
    
    if not args.dry_run:
        logger.info("\n✅ All positions closed! PnL locked in.")
        logger.info("Note: It may take a few seconds for positions to update in the UI")


if __name__ == "__main__":
    main()
