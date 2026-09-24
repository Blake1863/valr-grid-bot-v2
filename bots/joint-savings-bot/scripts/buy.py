#!/usr/bin/env python3
"""Manually buy SOL with all available ZAR."""

import json
import os
import sys
import time

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT_DIR)

from src.valr_client import ValrClient

config_path = os.path.join(BOT_DIR, "config.json")
with open(config_path) as f:
    config = json.load(f)

client = ValrClient(config["api_key"], config["api_secret"])

print(f"Available ZAR: R{client.get_zar_available():.2f}")
print(f"Available SOL: {client.get_sol_available():.6f}")

confirm = input("\nBuy SOL with all available ZAR? (yes/no): ")
if confirm.lower() != "yes":
    print("Aborted.")
    sys.exit(0)

import uuid
cid = f"js-manual-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}"
zar = str(client.get_zar_available())
print(f"\nPlacing MARKET BUY: {config['pair']} with R{zar}...")

sol_before = client.get_sol_available()
resp = client.place_market_buy(
    pair=config["pair"],
    quoteAmount=zar,
    customer_order_id=cid,
)

print(f"Order submitted: {resp.get('orderId')}")
print("Waiting for execution...")
time.sleep(5)

sol_after = client.get_sol_available()
bought = max(0, sol_after - sol_before)
print(f"\nSOL before: {sol_before:.6f}")
print(f"SOL after:  {sol_after:.6f}")
print(f"Bought:     {bought:.6f} SOL")
if bought > 0:
    print(f"Avg price:  R{float(zar)/bought:.2f}/SOL")
