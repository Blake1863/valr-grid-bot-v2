#!/usr/bin/env python3
"""
VALR Auto Buy & Sweep Bot
Detects ZAR deposits → Buys SOL → VALR Pay → Stake
"""

import json
import time
import hmac
import hashlib
import requests
import logging
from datetime import datetime
from pathlib import Path

# Setup logging
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "auto-sweep.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# Paths
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
SECRETS_SCRIPT = Path("/home/admin/.openclaw/secrets/secrets.py")

# Load config
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

# State tracking
def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"last_zar_balance": 0.0, "last_check": None}

def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

# Secrets management
def get_secret(name):
    """Fetch secret from Alibaba Secrets Manager via secrets.py"""
    import subprocess
    result = subprocess.run(
        ["python3", str(SECRETS_SCRIPT), "get", name],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to fetch secret {name}: {result.stderr}")
    return result.stdout.strip()

# VALR API authentication
def sign_request(verb, path, body="", subaccount_id=""):
    """HMAC-SHA512 signature for VALR API"""
    api_key = get_secret("valr_autobuy_api_key")
    api_secret = get_secret("valr_autobuy_api_secret")
    timestamp = str(int(time.time() * 1000))
    
    message = f"{timestamp}{verb}{path}{body}{subaccount_id}"
    signature = hmac.new(
        api_secret.encode(),
        message.encode(),
        hashlib.sha512
    ).hexdigest()
    
    return {
        "X-VALR-API-KEY": api_key,
        "X-VALR-SIGNATURE": signature,
        "X-VALR-TIMESTAMP": timestamp,
        "X-VALR-SUB-ACCOUNT-ID": subaccount_id
    }

def valr_request(verb, endpoint, body=None, subaccount_id="", include_subaccount_header=True):
    """Make authenticated request to VALR API"""
    url = f"https://api.valr.com{endpoint}"
    
    headers = sign_request(verb, endpoint, body or "", subaccount_id)
    if not include_subaccount_header:
        headers.pop("X-VALR-SUB-ACCOUNT-ID", None)
    
    if verb == "GET":
        resp = requests.get(url, headers=headers, params=body)
    elif verb == "POST":
        headers["Content-Type"] = "application/json"
        resp = requests.post(url, headers=headers, json=body)
    else:
        raise ValueError(f"Unsupported verb: {verb}")
    
    if resp.status_code >= 400:
        log.error(f"VALR API error {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    
    return resp.json()

# Balance detection
def get_zar_balance(subaccount_id):
    """Get ZAR balance for subaccount"""
    # Note: /v1/account/balances must NOT include subaccount header per VALR docs
    # Instead we use the subaccount-specific endpoint or filter
    balances = valr_request("GET", "/v1/account/balances", subaccount_id=subaccount_id)
    for balance in balances:
        if balance["currency"] == "ZAR":
            return float(balance["available"])
    return 0.0

def get_sol_balance(subaccount_id):
    """Get SOL balance for subaccount"""
    balances = valr_request("GET", "/v1/account/balances", subaccount_id=subaccount_id)
    for balance in balances:
        if balance["currency"] == "SOL":
            return float(balance["available"])
    return 0.0

# Order placement with slippage check
def get_orderbook(pair):
    """Get orderbook for pair"""
    resp = requests.get(f"https://api.valr.com/v1/public/{pair}/orderbook")
    resp.raise_for_status()
    return resp.json()

def parse_orderbook(ob):
    """Parse VALR orderbook (PascalCase: Bids/Asks)"""
    asks = ob.get("Asks", [])
    bids = ob.get("Bids", [])
    return asks, bids

def calculate_market_orders(zar_amount, pair, max_slippage_bps):
    """
    Calculate market order(s) with slippage protection.
    Returns list of order sizes (in ZAR) to execute.
    """
    orderbook = get_orderbook(pair)
    asks, bids = parse_orderbook(orderbook)
    
    # Calculate how much SOL we can buy within slippage limit
    asks = sorted(asks, key=lambda x: float(x["price"]))
    max_price = float(asks[0]["price"]) * (1 + max_slippage_bps / 10000)
    
    total_sol = 0.0
    total_zar = 0.0
    
    for ask in asks:
        price = float(ask["price"])
        qty = float(ask["quantity"])
        if price > max_price:
            break
        cost = price * qty
        if total_zar + cost > zar_amount:
            # Partial fill
            remaining_zar = zar_amount - total_zar
            partial_qty = remaining_zar / price
            total_sol += partial_qty
            total_zar += remaining_zar
            break
        total_sol += qty
        total_zar += cost
    
    if total_sol == 0:
        raise RuntimeError(f"No liquidity within {max_slippage_bps}bps slippage")
    
    log.info(f"Can buy {total_sol:.4f} SOL for {total_zar:.2f} ZAR within slippage limit")
    return total_sol, total_zar

def place_market_buy(pair, quantity, subaccount_id):
    """Place market buy order"""
    # VALR market order: POST /v1/orders/{pair}/market
    body = {
        "side": "BUY",
        "quantity": str(quantity),
        "orderType": "MARKET"
    }
    endpoint = f"/v1/orders/{pair}/market"
    return valr_request("POST", endpoint, body=body, subaccount_id=subaccount_id)

def execute_buy_with_splits(zar_amount, pair, subaccount_id):
    """Execute buy order, splitting if > threshold"""
    threshold = CONFIG["split_threshold_zar"]
    delay = CONFIG["split_delay_seconds"]
    max_retries = CONFIG["max_retries"]
    
    # Calculate SOL amount with slippage check
    sol_amount, actual_zar = calculate_market_orders(zar_amount, pair, CONFIG["max_slippage_bps"])
    
    orders_to_place = []
    if zar_amount > threshold:
        # Split into chunks
        chunks = []
        remaining = zar_amount
        while remaining > 0:
            chunk = min(remaining, threshold)
            chunks.append(chunk)
            remaining -= chunk
        log.info(f"Splitting {zar_amount:.2f} ZAR into {len(chunks)} orders: {chunks}")
        orders_to_place = chunks
    else:
        orders_to_place = [zar_amount]
    
    # Calculate SOL per chunk proportionally
    sol_per_chunk = [sol_amount * (chunk / actual_zar) for chunk in orders_to_place]
    
    # Execute orders with delay
    total_sol_bought = 0.0
    for i, (chunk_zar, chunk_sol) in enumerate(zip(orders_to_place, sol_per_chunk)):
        attempt = 0
        while attempt < max_retries:
            try:
                log.info(f"Placing order {i+1}/{len(orders_to_place)}: {chunk_sol:.4f} SOL")
                result = place_market_buy(pair, chunk_sol, subaccount_id)
                log.info(f"Order placed: {result}")
                total_sol_bought += chunk_sol
                
                if i < len(orders_to_place) - 1:
                    log.info(f"Waiting {delay}s before next order...")
                    time.sleep(delay)
                break
            except Exception as e:
                attempt += 1
                log.warning(f"Order failed (attempt {attempt}/{max_retries}): {e}")
                if attempt >= max_retries:
                    raise
                time.sleep(CONFIG["retry_delay_seconds"])
    
    return total_sol_bought

# VALR Pay transfer
def valr_pay_transfer(sol_amount, pay_id, subaccount_id):
    """Transfer SOL via VALR Pay"""
    # VALR Pay endpoint: POST /v1/pay
    # Use recipientPayId for VALR Pay ID transfers
    body = {
        "currency": "SOL",
        "amount": sol_amount,  # API expects float, not string
        "recipientPayId": pay_id,
        "anonymous": "false"
    }
    endpoint = "/v1/pay"
    return valr_request("POST", endpoint, body=body, subaccount_id=subaccount_id)

# Staking
def stake_sol(sol_amount, subaccount_id):
    """Stake SOL in VALR Earn"""
    # VALR Staking API: POST /v1/staking/stake
    body = {
        "currencySymbol": "SOL",
        "amount": str(sol_amount),  # String per API spec
        "earnType": "STAKE"
    }
    endpoint = "/v1/staking/stake"
    return valr_request("POST", endpoint, body=body, subaccount_id=subaccount_id)

# Telegram alert
def send_telegram_alert(message):
    """Send alert to Telegram topic"""
    # Use OpenClaw message tool via subprocess or direct API
    # For now, log it - we'll integrate with OpenClaw's message system
    log.error(f"TELEGRAM ALERT: {message}")
    # TODO: Integrate with OpenClaw message tool
    # This would call: message action=send channel=telegram target=<topic_id> message=<alert>

# Main loop
def main():
    log.info("🤖 VALR Auto Buy & Sweep Bot starting...")
    
    # Get subaccount ID from config or secrets
    # For now, we'll fetch it from secrets (Blake will add it)
    try:
        subaccount_id = get_secret("valr_autobuy_subaccount_id")
    except:
        log.warning("Subaccount ID not in secrets yet - using placeholder")
        subaccount_id = "PENDING"
    
    state = load_state()
    last_zar = state.get("last_zar_balance", 0.0)
    
    while True:
        try:
            # Check ZAR balance
            zar_balance = get_zar_balance(subaccount_id)
            log.info(f"ZAR balance: {zar_balance:.2f} (last: {last_zar:.2f})")
            
            # Detect deposit
            if zar_balance > last_zar + CONFIG["min_deposit_trigger_zar"]:
                deposit_amount = zar_balance - last_zar
                log.info(f"🎉 Deposit detected: {deposit_amount:.2f} ZAR")
                
                # Execute buy
                try:
                    sol_bought = execute_buy_with_splits(
                        deposit_amount,
                        CONFIG["pair"],
                        subaccount_id
                    )
                    log.info(f"✅ Bought {sol_bought:.4f} SOL")
                    
                    # Transfer via VALR Pay
                    attempt = 0
                    while attempt < CONFIG["max_retries"]:
                        try:
                            pay_result = valr_pay_transfer(
                                sol_bought,
                                CONFIG["valr_pay_id"],
                                subaccount_id
                            )
                            log.info(f"✅ VALR Pay transfer complete: {pay_result}")
                            break
                        except Exception as e:
                            attempt += 1
                            log.warning(f"VALR Pay failed (attempt {attempt}): {e}")
                            if attempt >= CONFIG["max_retries"]:
                                raise
                            time.sleep(CONFIG["retry_delay_seconds"])
                    
                    # Stake remaining SOL
                    sol_remaining = get_sol_balance(subaccount_id)
                    if sol_remaining > 0:
                        attempt = 0
                        while attempt < CONFIG["max_retries"]:
                            try:
                                stake_result = stake_sol(sol_remaining, subaccount_id)
                                log.info(f"✅ Staked {sol_remaining:.4f} SOL: {stake_result}")
                                break
                            except Exception as e:
                                attempt += 1
                                log.warning(f"Staking failed (attempt {attempt}): {e}")
                                if attempt >= CONFIG["max_retries"]:
                                    raise
                                time.sleep(CONFIG["retry_delay_seconds"])
                    
                    # Update state
                    state["last_zar_balance"] = 0.0  # Reset after sweep
                    state["last_check"] = datetime.now().isoformat()
                    save_state(state)
                    
                except Exception as e:
                    log.error(f"❌ Buy/sweep failed: {e}")
                    send_telegram_alert(f"❌ Auto-sweep failed: {e}")
            else:
                log.info("No new deposits detected")
            
            last_zar = zar_balance
            state["last_zar_balance"] = zar_balance
            save_state(state)
            
        except Exception as e:
            log.error(f"❌ Main loop error: {e}")
            send_telegram_alert(f"❌ Bot error: {e}")
        
        # Wait for next poll
        log.info(f"Sleeping for {CONFIG['poll_interval_seconds']}s...")
        time.sleep(CONFIG["poll_interval_seconds"])

if __name__ == "__main__":
    main()
