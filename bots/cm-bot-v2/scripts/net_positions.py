#!/usr/bin/env python3
"""
Net position imbalances between CM1 and CM2 subaccounts.
Runs every 5 minutes via cron.
"""

import argparse
import hmac
import hashlib
import json
import logging
import math
import os
import sys
import time
import uuid
from datetime import datetime

import requests

# Configuration
BASE_URL = "https://api.valr.com"
LOG_FILE = "/home/admin/.openclaw/workspace/bots/cm-bot-v2/logs/net-positions.log"

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
    """Generate VALR API authentication headers with optional subaccount impersonation."""
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
    """Fetch orderbook for a pair (no auth required)."""
    path = f"/v1/public/{pair}/orderbook"
    url = f"{BASE_URL}{path}"
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching orderbook for {pair}: {e}")
        return None


def get_balances(api_key, api_secret, subaccount_id=""):
    """Fetch account balances."""
    path = "/v1/account/balances"
    headers = make_headers(api_key, api_secret, "GET", path, "", subaccount_id)
    url = f"{BASE_URL}{path}"
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json()
    except requests.exceptions.RequestException:
        pass
    logger.warning("Could not fetch balances from any known endpoint")
    return []


def place_limit_order(api_key, api_secret, subaccount_id, pair, side, quantity, price, time_in_force="GTC", post_only=False, dry_run=False):
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
        logger.info(f"[DRY-RUN] Would place order: {side} {quantity} {pair} @ {price} (TOF={time_in_force}, postOnly={post_only})")
        return {"status": "dry-run", "order": body}
    
    try:
        response = requests.post(url, headers=headers, json=body, timeout=10)
        response.raise_for_status()
        result = response.json()
        logger.info(f"Order placed: {side} {quantity} {pair} @ {price} - OrderID: {result.get('orderId', 'N/A')}")
        return result
    except requests.exceptions.RequestException as e:
        logger.error(f"Error placing order: {e} - Response: {getattr(e, 'response', None)}")
        return None


def round_down(value, precision):
    """Round down to specified precision."""
    if precision >= 0:
        factor = 10 ** precision
        return math.floor(value * factor) / factor
    else:
        return round(value, -precision)


def format_quantity(qty, precision):
    """Format quantity with correct precision."""
    if precision == 0:
        return str(int(qty))
    else:
        return f"{qty:.{precision}f}"


def format_price(price, precision):
    """Format price with correct precision."""
    if precision == 0:
        return str(int(price))
    else:
        return f"{price:.{precision}f}"


def net_perp_positions(cm1_positions, cm2_positions, dry_run=False):
    """Net position imbalances for perpetual futures."""
    logger.info("=== Netting Perpetual Positions ===")
    
    # Build position maps
    cm1_map = {pos["pair"]: pos for pos in cm1_positions}
    cm2_map = {pos["pair"]: pos for pos in cm2_positions}
    
    for pair, spec in PAIR_SPECS.items():
        cm1_pos = cm1_map.get(pair, {})
        cm2_pos = cm2_map.get(pair, {})
        
        # Get quantities (positive for long, negative for short)
        cm1_qty = float(cm1_pos.get("quantity", 0) or 0)
        cm2_qty = float(cm2_pos.get("quantity", 0) or 0)
        
        # Calculate imbalance (difference in position)
        imbalance = cm1_qty - cm2_qty
        
        # Determine which account is more long
        if abs(imbalance) <= spec["min_qty"]:
            logger.info(f"{pair}: No significant imbalance (diff={imbalance:.6f}, min={spec['min_qty']})")
            continue
        
        # Calculate notional value using mid price from orderbook
        orderbook = get_orderbook(pair)
        if not orderbook or not orderbook.get("Asks"):
            logger.warning(f"{pair}: No orderbook data, skipping")
            continue
        
        ask_price = float(orderbook["Asks"][0]["price"])
        bid_price = float(orderbook["Bids"][0]["price"]) if orderbook.get("Bids") else ask_price
        mid_price = (ask_price + bid_price) / 2
        
        notional = abs(imbalance) * mid_price
        
        if notional <= 5:
            logger.info(f"{pair}: Notional too small (${notional:.2f} <= $5)")
            continue
        
        logger.info(f"{pair}: Imbalance detected - CM1: {cm1_qty}, CM2: {cm2_qty}, diff: {imbalance:.6f}, notional: ${notional:.2f}")
        
        # Round imbalance to precision
        trade_qty = round_down(abs(imbalance), spec["qty_precision"])
        trade_qty = max(trade_qty, spec["min_qty"])  # Ensure minimum qty
        
        if trade_qty < spec["min_qty"]:
            logger.info(f"{pair}: Rounded qty below minimum, skipping")
            continue
        
        formatted_qty = format_quantity(trade_qty, spec["qty_precision"])
        formatted_price = format_price(ask_price, spec["price_precision"])
        
        if imbalance > 0:
            # CM1 is more long: CM1 sells, CM2 buys
            logger.info(f"{pair}: CM1 more long by {formatted_qty} - CM1 SELLs, CM2 BUYs @ {formatted_price}")
            
            # CM1 places SELL limit order (GTC, postOnly=false)
            place_limit_order(MAIN_API_KEY, MAIN_API_SECRET, CM1_SUBACCOUNT_ID, pair, "SELL", formatted_qty, formatted_price, 
                            time_in_force="GTC", post_only=False, dry_run=dry_run)
            
            # Wait 10ms
            time.sleep(0.01)
            
            # CM2 places BUY IOC order
            place_limit_order(MAIN_API_KEY, MAIN_API_SECRET, CM2_SUBACCOUNT_ID, pair, "BUY", formatted_qty, formatted_price,
                            time_in_force="IOC", post_only=False, dry_run=dry_run)
        else:
            # CM2 is more long: CM2 sells, CM1 buys
            logger.info(f"{pair}: CM2 more long by {formatted_qty} - CM2 SELLs, CM1 BUYs @ {formatted_price}")
            
            # CM2 places SELL limit order (GTC, postOnly=false)
            place_limit_order(MAIN_API_KEY, MAIN_API_SECRET, CM2_SUBACCOUNT_ID, pair, "SELL", formatted_qty, formatted_price,
                            time_in_force="GTC", post_only=False, dry_run=dry_run)
            
            # Wait 10ms
            time.sleep(0.01)
            
            # CM1 places BUY IOC order
            place_limit_order(MAIN_API_KEY, MAIN_API_SECRET, CM1_SUBACCOUNT_ID, pair, "BUY", formatted_qty, formatted_price,
                            time_in_force="IOC", post_only=False, dry_run=dry_run)


def net_link_balance(dry_run=False):
    """Net LINK balance imbalance between CM1 and CM2."""
    logger.info("=== Netting LINK Balance ===")
    
    cm1_balances = get_balances(MAIN_API_KEY, MAIN_API_SECRET, CM1_SUBACCOUNT_ID)
    cm2_balances = get_balances(MAIN_API_KEY, MAIN_API_SECRET, CM2_SUBACCOUNT_ID)
    
    # Find LINK balance
    cm1_link = next((b for b in cm1_balances if b.get("asset") == "LINK"), {})
    cm2_link = next((b for b in cm2_balances if b.get("asset") == "LINK"), {})
    
    cm1_available = float(cm1_link.get("available", 0) or 0)
    cm2_available = float(cm2_link.get("available", 0) or 0)
    
    imbalance = cm1_available - cm2_available
    
    if abs(imbalance) <= 0.04:
        logger.info(f"LINK: No significant balance imbalance (diff={imbalance:.6f} LINK)")
        return
    
    logger.info(f"LINK: Balance imbalance - CM1: {cm1_available:.6f}, CM2: {cm2_available:.6f}, diff: {imbalance:.6f} LINK")
    
    # For LINKZAR, we'd need to place a spot order
    # This is a simplified version - in production you'd want to check LINKZAR orderbook
    orderbook = get_orderbook("LINKZAR")
    if not orderbook or not orderbook.get("Asks"):
        logger.warning("LINKZAR: No orderbook data, skipping balance net")
        return
    
    ask_price = float(orderbook["Asks"][0]["price"])
    trade_qty = round_down(abs(imbalance), 8)  # LINK typically has 8 decimal precision
    trade_qty = max(trade_qty, 0.04)  # Minimum threshold
    
    formatted_qty = f"{trade_qty:.8f}"
    formatted_price = f"{ask_price:.2f}"
    
    if imbalance > 0:
        # CM1 has more LINK: CM1 sells, CM2 buys
        logger.info(f"LINK: CM1 has {formatted_qty} more - CM1 SELLs, CM2 BUYs @ {formatted_price}")
        
        place_limit_order(MAIN_API_KEY, MAIN_API_SECRET, CM1_SUBACCOUNT_ID, "LINKZAR", "SELL", formatted_qty, formatted_price,
                         time_in_force="GTC", post_only=False, dry_run=dry_run)
        
        time.sleep(0.01)
        
        place_limit_order(MAIN_API_KEY, MAIN_API_SECRET, CM2_SUBACCOUNT_ID, "LINKZAR", "BUY", formatted_qty, formatted_price,
                         time_in_force="IOC", post_only=False, dry_run=dry_run)
    else:
        # CM2 has more LINK: CM2 sells, CM1 buys
        logger.info(f"LINK: CM2 has {formatted_qty} more - CM2 SELLs, CM1 BUYs @ {formatted_price}")
        
        place_limit_order(MAIN_API_KEY, MAIN_API_SECRET, CM2_SUBACCOUNT_ID, "LINKZAR", "SELL", formatted_qty, formatted_price,
                         time_in_force="GTC", post_only=False, dry_run=dry_run)
        
        time.sleep(0.01)
        
        place_limit_order(MAIN_API_KEY, MAIN_API_SECRET, CM1_SUBACCOUNT_ID, "LINKZAR", "BUY", formatted_qty, formatted_price,
                         time_in_force="IOC", post_only=False, dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser(description="Net position imbalances between CM1 and CM2")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without placing orders")
    args = parser.parse_args()
    
    logger.info("=" * 60)
    logger.info(f"Starting position netting (dry_run={args.dry_run})")
    logger.info("=" * 60)
    
    # Fetch positions for both accounts
    logger.info("Fetching CM1 positions...")
    cm1_positions = get_open_positions(MAIN_API_KEY, MAIN_API_SECRET, CM1_SUBACCOUNT_ID)
    logger.info(f"CM1 has {len(cm1_positions)} open positions")
    
    logger.info("Fetching CM2 positions...")
    cm2_positions = get_open_positions(MAIN_API_KEY, MAIN_API_SECRET, CM2_SUBACCOUNT_ID)
    logger.info(f"CM2 has {len(cm2_positions)} open positions")
    
    # Net perpetual positions
    net_perp_positions(cm1_positions, cm2_positions, dry_run=args.dry_run)
    
    # Net LINK balance
    net_link_balance(dry_run=args.dry_run)
    
    logger.info("=" * 60)
    logger.info("Position netting complete")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
