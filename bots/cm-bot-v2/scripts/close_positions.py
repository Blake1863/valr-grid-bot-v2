#!/usr/bin/env python3
"""
Close all open futures positions on CM1 and CM2, and cancel all open orders.
Uses market-side IOC limit orders at 1% slippage to guarantee fills.

Usage:
  python3 close_positions.py --dry-run   # Show what would be done
  python3 close_positions.py             # Actually execute
"""
import hmac as hmaclib
import hashlib
import time
import urllib.request
import urllib.error
import json
import sys
import argparse
from pathlib import Path

PAIRS = [
    "BTCUSDTPERP",
    "ETHUSDTPERP",
    "SOLUSDTPERP",
    "XRPUSDTPERP",
    "DOGEUSDTPERP",
    "AVAXUSDTPERP",
]

# Price precision per pair (decimal places)
PRICE_PRECISION = {
    "BTCUSDTPERP": 0,
    "ETHUSDTPERP": 1,
    "SOLUSDTPERP": 2,
    "XRPUSDTPERP": 4,
    "DOGEUSDTPERP": 5,
    "AVAXUSDTPERP": 3,
}

QTY_PRECISION = {
    "BTCUSDTPERP": 4,
    "ETHUSDTPERP": 3,
    "SOLUSDTPERP": 2,
    "XRPUSDTPERP": 1,
    "DOGEUSDTPERP": 0,
    "AVAXUSDTPERP": 2,
}


def sign(secret: str, method: str, path: str, body: str = "", subaccount: str = "") -> tuple[str, str]:
    ts = str(int(time.time() * 1000))
    msg = ts + method + path + body + (subaccount or "")
    sig = hmaclib.new(secret.encode(), msg.encode(), hashlib.sha512).hexdigest()
    return ts, sig


def api_get(key: str, secret: str, path: str, subid: str = "") -> dict | list:
    ts, sig = sign(secret, "GET", path, "", subid)
    url = f"https://api.valr.com{path}"
    req = urllib.request.Request(url, headers={
        "X-VALR-API-KEY": key,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
        "X-VALR-SUB-ACCOUNT-ID": subid,
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"GET {path} failed ({e.code}): {body}")


def api_delete(key: str, secret: str, path: str, body_dict: dict, subid: str = "") -> dict | list:
    body = json.dumps(body_dict)
    ts, sig = sign(secret, "DELETE", path, body, subid)
    url = f"https://api.valr.com{path}"
    req = urllib.request.Request(url, data=body.encode(), method="DELETE", headers={
        "X-VALR-API-KEY": key,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
        "X-VALR-SUB-ACCOUNT-ID": subid,
        "Content-Type": "application/json",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read()) if resp.read() else {}
    except urllib.error.HTTPError as e:
        body_resp = e.read().decode()
        raise RuntimeError(f"DELETE {path} failed ({e.code}): {body_resp}")


def api_post(key: str, secret: str, path: str, body_dict: dict, subid: str = "") -> dict | list:
    body = json.dumps(body_dict)
    ts, sig = sign(secret, "POST", path, body, subid)
    url = f"https://api.valr.com{path}"
    req = urllib.request.Request(url, data=body.encode(), method="POST", headers={
        "X-VALR-API-KEY": key,
        "X-VALR-SIGNATURE": sig,
        "X-VALR-TIMESTAMP": ts,
        "X-VALR-SUB-ACCOUNT-ID": subid,
        "Content-Type": "application/json",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_resp = e.read().decode()
        raise RuntimeError(f"POST {path} failed ({e.code}): {body_resp}")


def get_orderbook_mid(pair: str) -> float:
    """Fetch best bid/ask from public orderbook REST endpoint."""
    url = f"https://api.valr.com/v1/public/{pair}/orderbook"
    req = urllib.request.Request(url)
    resp = urllib.request.urlopen(req, timeout=10)
    ob = json.loads(resp.read())
    bid = float(ob["Bids"][0]["price"])
    ask = float(ob["Asks"][0]["price"])
    return bid, ask


def round_price(price: float, pair: str) -> str:
    precision = PRICE_PRECISION.get(pair, 2)
    return f"{price:.{precision}f}"


def round_qty(qty: float, pair: str) -> str:
    precision = QTY_PRECISION.get(pair, 2)
    return f"{qty:.{precision}f}"


def load_env() -> dict:
    env_path = Path(__file__).parent.parent / ".env"
    env = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def cancel_open_orders(key: str, secret: str, subid: str, label: str, pair: str, dry_run: bool):
    try:
        orders = api_get(key, secret, f"/v1/orders/open?pair={pair}", subid)
    except Exception as e:
        print(f"  [WARN] Could not fetch open orders for {label} {pair}: {e}")
        return 0

    if not orders:
        return 0

    cancelled = 0
    for order in orders:
        order_id = order.get("orderId", "")
        side = order.get("side", "")
        price = order.get("price", "")
        qty = order.get("remainingQuantity", "")
        print(f"  {'[DRY] Would cancel' if dry_run else 'Cancelling'} {label} open order: {pair} {side} {qty} @ {price} (id={order_id[:8]})")
        if not dry_run:
            try:
                api_delete(key, secret, "/v1/orders/order", {"orderId": order_id, "pair": pair}, subid)
                print(f"    ✓ Cancelled")
                cancelled += 1
            except Exception as e:
                print(f"    ✗ Cancel failed: {e}")
        else:
            cancelled += 1
    return cancelled


def close_position(key: str, secret: str, subid: str, label: str, pos: dict, dry_run: bool):
    pair = pos.get("pair", pos.get("currencyPair", ""))
    side = pos.get("side", "").lower()  # "buy" or "sell"
    qty_str = pos.get("quantity", "0")
    qty = float(qty_str)

    if qty <= 0:
        return

    # To close a long (buy position), we sell. To close a short (sell position), we buy.
    close_side = "SELL" if side == "buy" else "BUY"

    # Get live price and add 1% slippage for IOC to guarantee fill
    try:
        bid, ask = get_orderbook_mid(pair)
    except Exception as e:
        print(f"  [WARN] Could not fetch price for {pair}: {e}")
        return

    # Closing buy → we sell → use bid minus 1% slippage (aggressive sell)
    # Closing sell → we buy → use ask plus 1% slippage (aggressive buy)
    if close_side == "SELL":
        close_price = bid * 0.99
    else:
        close_price = ask * 1.01

    price_str = round_price(close_price, pair)
    qty_str_rounded = round_qty(qty, pair)

    print(f"  {'[DRY] Would close' if dry_run else 'Closing'} {label} {pair}: {side} {qty_str_rounded} → {close_side} IOC @ {price_str} (bid={bid}, ask={ask})")

    if not dry_run:
        import uuid
        order_body = {
            "side": close_side,
            "quantity": qty_str_rounded,
            "price": price_str,
            "pair": pair,
            "postOnly": False,
            "timeInForce": "IOC",
            "customerOrderId": str(uuid.uuid4()),
        }
        try:
            result = api_post(key, secret, "/v2/orders/limit", order_body, subid)
            print(f"    ✓ Close order placed: id={result.get('id', 'n/a')[:8]}")
        except Exception as e:
            print(f"    ✗ Close order failed: {e}")


def process_account(key: str, secret: str, subid: str, label: str, dry_run: bool):
    print(f"\n{'='*50}")
    print(f"  {label} (subaccount {subid})")
    print(f"{'='*50}")

    # Step 1: Cancel all open orders first (frees up locked margin)
    print(f"\n[Step 1] Cancelling open orders on {label}...")
    total_cancelled = 0
    for pair in PAIRS:
        cancelled = cancel_open_orders(key, secret, subid, label, pair, dry_run)
        total_cancelled += cancelled
        time.sleep(0.15)
    print(f"  Total cancelled: {total_cancelled}")

    if not dry_run and total_cancelled > 0:
        print("  Waiting 1s for cancellations to settle...")
        time.sleep(1)

    # Step 2: Close open positions
    print(f"\n[Step 2] Closing open positions on {label}...")
    try:
        positions = api_get(key, secret, "/v1/positions/open", subid)
    except Exception as e:
        print(f"  [ERROR] Could not fetch positions: {e}")
        return

    if not positions:
        print("  No open positions.")
        return

    for pos in positions:
        pair = pos.get("pair", pos.get("currencyPair", "unknown"))
        side = pos.get("side", "?")
        qty = pos.get("quantity", "0")
        upnl = pos.get("unrealisedPnl", "0")
        print(f"\n  Found: {pair} {side} qty={qty} uPnL={upnl}")
        close_position(key, secret, subid, label, pos, dry_run)
        time.sleep(0.2)


def main():
    parser = argparse.ArgumentParser(description="Close all CM1/CM2 positions and open orders")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without executing")
    args = parser.parse_args()

    dry_run = args.dry_run
    if dry_run:
        print("🔍 DRY RUN MODE — no orders will be placed or cancelled")
    else:
        print("⚠️  LIVE MODE — this will cancel orders and close positions!")
        print("   Press Ctrl+C within 3s to abort...")
        try:
            time.sleep(3)
        except KeyboardInterrupt:
            print("\nAborted.")
            sys.exit(0)

    env = load_env()
    key = env["MAIN_API_KEY"]
    secret = env["MAIN_API_SECRET"]
    cm1 = env["CM1_SUBACCOUNT_ID"]
    cm2 = env["CM2_SUBACCOUNT_ID"]

    process_account(key, secret, cm1, "CM1", dry_run)
    process_account(key, secret, cm2, "CM2", dry_run)

    print("\n✅ Done.")


if __name__ == "__main__":
    main()
