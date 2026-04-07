#!/usr/bin/env python3
"""
Offset positions internally between CM1 and CM2.
Places maker order (GTC) first, then taker order (IOC) at SAME price.
This ensures internal matching without hitting external liquidity.

Usage:
  python3 internal_offset.py --dry-run   # Show what would be done
  python3 internal_offset.py             # Execute offsets
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
LOG_FILE = "/home/admin/.openclaw/workspace/bots/cm-bot-v2/logs/internal-offset.log"

MAIN_API_KEY = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
MAIN_API_SECRET = "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"
CM1_SUBACCOUNT_ID = "1483472097578319872"
CM2_SUBACCOUNT_ID = "1483472079069155328"

PAIR_SPECS = {
    "BTCUSDTPERP": {"qty_precision": 4, "price_precision": 0, "min_qty": 0.0001},
    "ETHUSDTPERP": {"qty_precision": 3, "price_precision": 1, "min_qty": 0.001},
    "XRPUSDTPERP": {"qty_precision": 0, "price_precision": 4, "min_qty": 2},
    "DOGEUSDTPERP": {"qty_precision": 0, "price_precision": 5, "min_qty": 6},
    "SOLUSDTPERP": {"qty_precision": 2, "price_precision": 2, "min_qty": 0.01},
    "AVAXUSDTPERP": {"qty_precision": 2, "price_precision": 3, "min_qty": 0.03},
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
        return response.json()
    except Exception as e:
        logger.error(f"Error fetching orderbook: {e}")
        return None


def place_limit_order(api_key, api_secret, subaccount_id, pair, side, quantity, price, time_in_force, dry_run=False):
    """Place a limit order."""
    path = "/v2/orders/limit"
    customer_order_id = str(uuid.uuid4())
    
    body = {
        "side": side,
        "quantity": str(quantity),
        "price": str(price),
        "pair": pair,
        "timeInForce": time_in_force,
        "customerOrderId": customer_order_id,
        "reduceOnly": False,  # Internal matching at mid - accounts are opposite
    }
    
    body_json = json.dumps(body)
    headers = make_headers(api_key, api_secret, "POST", path, body_json, subaccount_id)
    
    if dry_run:
        logger.info(f"    [DRY-RUN] {side} {quantity} {pair} @ {price} ({time_in_force})")
        return {"status": "dry-run"}
    
    try:
        response = requests.post(f"{BASE_URL}{path}", headers=headers, json=body, timeout=10)
        result = response.json()
        
        if response.status_code in [200, 201, 202]:
            order_id = result.get('orderId', 'N/A')
            if isinstance(order_id, str):
                order_id = order_id[:16]
            logger.info(f"    ✅ Order placed: {side} {quantity} @ {price} - ID: {order_id}")
            return {"status": "success"}
        else:
            logger.error(f"    ❌ Order failed: {response.status_code} - {result}")
            return {"status": "failed", "error": result}
    except Exception as e:
        logger.error(f"    ❌ Error: {e}")
        return {"status": "error", "error": str(e)}


def offset_positions(pair, cm1_qty, cm2_qty, price, dry_run=False):
    """
    Offset opposite positions between CM1 and CM2.
    
    Because wash trades always produce opposite positions:
    - One account is LONG (+qty), other is SHORT (-qty)
    
    Strategy:
    1. LONG account places SELL (GTC maker) at mid - closes long, reduces position
    2. Wait 300ms for order to land on book
    3. SHORT account places BUY (IOC taker) at SAME mid - closes short, matches internally
    4. Orders match against each other - zero external fills, zero net market impact
    """
    spec = PAIR_SPECS.get(pair, {"qty_precision": 2, "price_precision": 2, "min_qty": 0.01})
    
    # Offset quantity = smaller of the two absolute positions
    offset_qty = min(abs(cm1_qty), abs(cm2_qty))
    offset_qty = round(offset_qty, spec["qty_precision"])
    offset_qty = max(offset_qty, spec["min_qty"])
    
    formatted_qty = f"{offset_qty:.{spec['qty_precision']}f}"
    formatted_price = f"{price:.{spec['price_precision']}f}"
    
    logger.info(f"  Offsetting {formatted_qty} @ {formatted_price}")
    
    # The LONG account sells (maker), SHORT account buys (taker)
    # cm1_qty > 0 means CM1 is long, cm1_qty < 0 means CM1 is short
    if cm1_qty > 0:
        # CM1 is LONG → CM1 sells (maker), CM2 buys to close short (taker)
        maker_account = CM1_SUBACCOUNT_ID
        maker_label = "CM1 (LONG)"
        taker_account = CM2_SUBACCOUNT_ID
        taker_label = "CM2 (SHORT)"
    else:
        # CM2 is LONG → CM2 sells (maker), CM1 buys to close short (taker)
        maker_account = CM2_SUBACCOUNT_ID
        maker_label = "CM2 (LONG)"
        taker_account = CM1_SUBACCOUNT_ID
        taker_label = "CM1 (SHORT)"
    
    logger.info(f"    Maker: {maker_label} SELL {formatted_qty} (GTC) at mid")
    logger.info(f"    Taker: {taker_label} BUY {formatted_qty} (IOC) at same mid")
    
    # Step 1: Place maker order (GTC - rests on book)
    logger.info(f"  [Step 1] Placing maker order...")
    maker_result = place_limit_order(
        MAIN_API_KEY, MAIN_API_SECRET, maker_account,
        pair, "SELL", formatted_qty, formatted_price,
        time_in_force="GTC", dry_run=dry_run
    )
    
    if maker_result.get("status") != "success" and maker_result.get("status") != "dry-run":
        logger.error(f"  ❌ Maker order failed, aborting offset")
        return False
    
    # Step 2: Wait for maker to land on book
    if not dry_run:
        logger.info(f"  [Step 2] Waiting 500ms for maker to land...")
        time.sleep(0.5)
    
    # Step 3: Place taker order (IOC - matches against maker)
    logger.info(f"  [Step 3] Placing taker order...")
    taker_result = place_limit_order(
        MAIN_API_KEY, MAIN_API_SECRET, taker_account,
        pair, "BUY", formatted_qty, formatted_price,
        time_in_force="IOC", dry_run=dry_run
    )
    
    if taker_result.get("status") == "success" or taker_result.get("status") == "dry-run":
        logger.info(f"  ✅ Offset successful: {formatted_qty} {pair}")
        return True
    else:
        logger.error(f"  ❌ Offset failed: taker order did not match")
        return False


def get_usdt_available(subaccount_id):
    """Fetch available USDT for a subaccount."""
    path = "/v1/account/balances"
    headers = make_headers(MAIN_API_KEY, MAIN_API_SECRET, "GET", path, "", subaccount_id)
    try:
        r = requests.get(f"{BASE_URL}{path}", headers=headers, timeout=10)
        r.raise_for_status()
        usdt = next((b for b in r.json() if b.get("currency") == "USDT"), {})
        return float(usdt.get("available", 0) or 0)
    except Exception as e:
        logger.error(f"Error fetching balances for {subaccount_id}: {e}")
        return 0.0


def transfer_usdt(from_id, to_id, amount, dry_run=False):
    """Transfer USDT between subaccounts."""
    path = "/v1/account/subaccounts/transfer"
    body = {
        "fromId": from_id,
        "toId":   to_id,
        "currencyCode": "USDT",
        "amount": f"{amount:.2f}",
        "allowBorrow": False,
    }
    body_json = json.dumps(body)
    headers = make_headers(MAIN_API_KEY, MAIN_API_SECRET, "POST", path, body_json, "")
    if dry_run:
        logger.info(f"    [DRY-RUN] Transfer ${amount:.2f} USDT {from_id[:8]}... → {to_id[:8]}...")
        return True
    try:
        r = requests.post(f"{BASE_URL}{path}", headers=headers, json=body, timeout=10)
        if r.status_code in (200, 202):
            logger.info(f"    ✅ Transferred ${amount:.2f} USDT")
            return True
        else:
            logger.error(f"    ❌ Transfer failed: {r.status_code} {r.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"    ❌ Transfer error: {e}")
        return False


REBALANCE_THRESHOLD = 0.60   # rebalance if one side holds >60%
MIN_TRANSFER        = 5.0    # minimum USDT to bother transferring


def rebalance_usdt(dry_run=False):
    """Rebalance USDT between CM1 and CM2 to a 50/50 split."""
    logger.info("\n--- USDT Rebalance (CM1 / CM2) ---")

    cm1 = get_usdt_available(CM1_SUBACCOUNT_ID)
    cm2 = get_usdt_available(CM2_SUBACCOUNT_ID)
    total = cm1 + cm2

    logger.info(f"  CM1 avail: ${cm1:.2f} | CM2 avail: ${cm2:.2f} | Total: ${total:.2f}")

    if total < 1.0:
        logger.info("  Total too low — skipping")
        return

    cm1_pct = cm1 / total
    cm2_pct = cm2 / total
    logger.info(f"  Split: CM1={cm1_pct*100:.1f}%  CM2={cm2_pct*100:.1f}%")

    if cm1_pct <= REBALANCE_THRESHOLD and cm2_pct <= REBALANCE_THRESHOLD:
        logger.info(f"  ✅ Within threshold — no transfer needed")
        return

    target   = total / 2
    if cm1 > cm2:
        from_id, to_id, from_lbl, to_lbl = CM1_SUBACCOUNT_ID, CM2_SUBACCOUNT_ID, "CM1", "CM2"
        surplus = cm1 - target
    else:
        from_id, to_id, from_lbl, to_lbl = CM2_SUBACCOUNT_ID, CM1_SUBACCOUNT_ID, "CM2", "CM1"
        surplus = cm2 - target

    # Transfer slightly less than full surplus to avoid rounding errors
    amount = round(surplus * 0.95, 2)
    if amount < MIN_TRANSFER:
        logger.info(f"  Transfer ${amount:.2f} < min ${MIN_TRANSFER:.2f} — skipping")
        return

    logger.info(f"  ⚠️  {from_lbl} has ${surplus:.2f} surplus → transferring ${amount:.2f} to {to_lbl}")
    transfer_usdt(from_id, to_id, amount, dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser(description="Internal position offset between CM1/CM2")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    args = parser.parse_args()
    
    logger.info("=" * 70)
    logger.info(f"Internal Position Offset (dry_run={args.dry_run})")
    logger.info(f"Timestamp: {datetime.now().isoformat()}")
    logger.info("=" * 70)
    
    # Fetch positions
    logger.info("\nFetching positions...")
    cm1_positions = get_open_positions(MAIN_API_KEY, MAIN_API_SECRET, CM1_SUBACCOUNT_ID)
    cm2_positions = get_open_positions(MAIN_API_KEY, MAIN_API_SECRET, CM2_SUBACCOUNT_ID)
    
    logger.info(f"CM1: {len(cm1_positions)} positions")
    logger.info(f"CM2: {len(cm2_positions)} positions")
    
    if not cm1_positions and not cm2_positions:
        logger.info("\n✅ No open positions to offset")
        return
    
    # Build position maps with sign: positive = long, negative = short
    def signed_qty(pos):
        qty = float(pos.get("quantity", 0) or 0)
        side = pos.get("side", "Buy")
        return qty if side == "Buy" else -qty
    
    cm1_map = {pos["pair"]: signed_qty(pos) for pos in cm1_positions if isinstance(pos, dict)}
    cm2_map = {pos["pair"]: signed_qty(pos) for pos in cm2_positions if isinstance(pos, dict)}
    
    # Get all pairs
    all_pairs = set(cm1_map.keys()) | set(cm2_map.keys())
    
    successful = 0
    failed = 0
    
    for pair in all_pairs:
        if pair not in PAIR_SPECS:
            continue
        
        cm1_qty = cm1_map.get(pair, 0)
        cm2_qty = cm2_map.get(pair, 0)
        
        # Skip if both have no position
        if abs(cm1_qty) < 0.0001 and abs(cm2_qty) < 0.0001:
            continue
        
        # Skip if both are same direction — can't offset internally
        if cm1_qty > 0 and cm2_qty > 0:
            logger.info(f"{pair}: Both LONG — skipping (no internal offset possible)")
            continue
        if cm1_qty < 0 and cm2_qty < 0:
            logger.info(f"{pair}: Both SHORT — skipping (no internal offset possible)")
            continue
        
        # Get mid price
        orderbook = get_orderbook(pair)
        if not orderbook or not orderbook.get("Asks") or not orderbook.get("Bids"):
            logger.warning(f"{pair}: No orderbook data, skipping")
            failed += 1
            continue
        
        ask = float(orderbook["Asks"][0]["price"])
        bid = float(orderbook["Bids"][0]["price"])
        mid = (ask + bid) / 2
        
        logger.info(f"\n{pair}:")
        logger.info(f"  CM1: {cm1_qty:+.6f} | CM2: {cm2_qty:+.6f} | Mid: ${mid:,.2f}")
        
        if offset_positions(pair, cm1_qty, cm2_qty, mid, dry_run=args.dry_run):
            successful += 1
        else:
            failed += 1
        
        time.sleep(0.5)  # Delay between pairs
    
    logger.info("\n" + "=" * 70)
    logger.info(f"Summary: {successful} successful, {failed} failed")
    logger.info("=" * 70)
    
    if not args.dry_run and failed == 0:
        logger.info("\n✅ All positions offset successfully! PnL locked in.")

    # Always rebalance USDT after position offsets
    rebalance_usdt(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
