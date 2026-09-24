#!/usr/bin/env python3
"""Setup ETHBTC wash-trading bucket: buy BTC+ETH on main, split to CMSBTC1/2, draft config."""
import asyncio, json, sys, os, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from creds import load
from src.valr_rest import ValrRest

CMSBTC1 = "1518603875676921856"
CMSBTC2 = "1518603693873459200"

# Target ~$35/pair total = ~$17.50/sub per currency
# BTC @ ~$65,151 → $35 = 0.000537 BTC total → 0.000269/sub
# ETH @ ~$1,769 → $35 = 0.0198 ETH total → 0.0099/sub
TARGET_USD_PER_CURRENCY = 35.0
BTC_USDT_SPEND = 35.0   # buy $35 BTC
ETH_USDT_SPEND = 35.0   # buy $35 ETH

async def main():
    creds = load()
    rest = ValrRest(
        api_key=creds["MAIN_API_KEY"],
        api_secret=creds["MAIN_API_SECRET"],
    )

    # Step 1: Check main balances
    bals = await rest.request("GET", "/v1/account/balances")
    btc_bal = next((b for b in bals if b["currency"] == "BTC"), None)
    eth_bal = next((b for b in bals if b["currency"] == "ETH"), None)
    usdt_bal = next((b for b in bals if b["currency"] == "USDT"), None)

    btc_avail = float(btc_bal["available"]) if btc_bal else 0
    eth_avail = float(eth_bal["available"]) if eth_bal else 0
    usdt_avail = float(usdt_bal["available"]) if usdt_bal else 0

    print(f"Main balances: BTC={btc_avail}, ETH={eth_avail}, USDT={usdt_avail}")

    # Step 2: Buy BTC if needed
    if btc_avail < 0.0004:
        print(f"Buying ${BTC_USDT_SPEND} BTC via market BUY BTCUSDT...")
        resp = await rest.request("POST", "/v2/orders/market", body={
            "side": "BUY",
            "quoteAmount": str(BTC_USDT_SPEND),
            "pair": "BTCUSDT"
        })
        print(f"  Order ID: {resp}")
        await asyncio.sleep(3)

    # Step 3: Buy ETH if needed
    if eth_avail < 0.015:
        print(f"Buying ${ETH_USDT_SPEND} ETH via market BUY ETHUSDT...")
        resp = await rest.request("POST", "/v2/orders/market", body={
            "side": "BUY",
            "quoteAmount": str(ETH_USDT_SPEND),
            "pair": "ETHUSDT"
        })
        print(f"  Order ID: {resp}")
        await asyncio.sleep(3)

    # Step 4: Re-check main balances after buys
    bals = await rest.request("GET", "/v1/account/balances")
    btc_bal = next((b for b in bals if b["currency"] == "BTC"), None)
    eth_bal = next((b for b in bals if b["currency"] == "ETH"), None)
    btc_total = float(btc_bal["available"]) if btc_bal else 0
    eth_total = float(eth_bal["available"]) if eth_bal else 0
    print(f"After buys: BTC={btc_total}, ETH={eth_total}")

    if btc_total < 0.0004 or eth_total < 0.015:
        print("ERROR: Insufficient balance after buys. Aborting transfers.")
        return

    # Step 5: Transfer to subs (50/50)
    half_btc = f"{btc_total / 2:.8f}"
    half_eth = f"{eth_total / 2:.6f}"

    transfers = []
    for sub_id in [CMSBTC1, CMSBTC2]:
        transfers.append(("BTC", half_btc, sub_id))
        transfers.append(("ETH", half_eth, sub_id))

    for curr, amount, sub_id in transfers:
        print(f"Transferring {amount} {curr} → sub {sub_id}...")
        resp = await rest.request("POST", "/v1/account/subaccounts/transfer", body={
            "fromId": 0,
            "toId": int(sub_id),
            "currencyCode": curr,
            "amount": amount,
            "allowBorrow": False
        })
        print(f"  Transfer ID: {resp.get('id', resp)}")
        await asyncio.sleep(1)

    # Step 6: Verify sub balances
    print("\n=== Sub Balances ===")
    for sub_id, name in [(CMSBTC1, "CMSBTC1"), (CMSBTC2, "CMSBTC2")]:
        bals = await rest.request("GET", "/v1/account/balances", subaccount_id=sub_id)
        for b in bals:
            if b["currency"] in ("BTC", "ETH"):
                print(f"  {name}: {b['currency']} = {b['available']}")

    print("\n✅ ETHBTC bucket setup complete. Draft config:")
    print(json.dumps({
        "_comment": "BTC quote bucket for valr-cm-spot@btc",
        "global": {
            "instance_name": "valr-cm-spot-btc",
            "account_a_id": CMSBTC1,
            "account_b_id": CMSBTC2,
            "account_a_name": "CMSBTC1",
            "account_b_name": "CMSBTC2",
            "rate_limit_per_sec": 20,
            "summary_interval_seconds": 300,
            "rebalance_interval_cycles": 6,
            "rebalance_threshold_pct": 0.6,
            "min_transfer_value_usd": 1.0,
            "external_fill_alert_threshold": 0.1,
            "leak_window_size": 100,
            "leak_min_samples": 20,
            "alert_cooldown_seconds": 1800,
            "stagger_seconds": 1.5,
            "use_account_ws": True,
            "telegram_chat_id": "telegram:7018990694",
            "cancel_all_on_startup": True
        },
        "defaults": {
            "cycle_interval_seconds": 20.0,
            "min_spread_ticks": 2,
            "max_spread_bps": 200,
            "print_value_usd_min": 1.5,
            "print_value_usd_max": 4.0,
            "inventory_floor_usd": 5.0,
            "max_consecutive_same_maker": 5,
            "quote_to_usd": 65151.0
        },
        "pairs": {
            "ETHBTC": {
                "enabled": True,
                "cycle_interval_seconds": 20.0,
                "min_spread_ticks": 2
            }
        }
    }, indent=4))

if __name__ == "__main__":
    asyncio.run(main())
