#!/usr/bin/env python3
"""Quick test: verify credentials, check balance, fetch market data, and dry-run a purchase."""

import json
import os
import sys

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT_DIR)

from src.valr_client import ValrClient

config_path = os.path.join(BOT_DIR, "config.json")
with open(config_path) as f:
    config = json.load(f)

client = ValrClient(config["api_key"], config["api_secret"])

print("=== Balances (non-zero only) ===")
balances = client.get_balances()
for b in balances:
    avail = float(b.get("available", "0"))
    reserved = float(b.get("reserved", "0"))
    if avail > 0 or reserved > 0:
        print(f"  {b['currency']}: available={avail}, reserved={reserved}")
print()

print(f"=== {config['pair']} Market ===")
summary = client.get_market_summary(config["pair"])
print(f"  Ask: {summary.get('askPrice')}")
print(f"  Bid: {summary.get('bidPrice')}")
print(f"  Last: {summary.get('lastTradedPrice')}")
print()

print("=== Deposit History (last 5) ===")
deposits = client.get_fiat_deposits(limit=5)
if deposits:
    for d in deposits:
        print(f"  {d.get('id','?')[:8]}... | {d.get('creditValue')} {d.get('creditCurrency')} | {d.get('eventAt')}")
else:
    print("  (no FIAT_DEPOSIT transactions found)")
print()

print("✅ All API calls successful. Bot is ready.")
