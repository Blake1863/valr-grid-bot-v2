#!/usr/bin/env python3
"""
Bootstrap opening balances for the monthly reconciliation.
Fetches current balances across all 4 subaccounts and saves them
as the closing balance for the current month (used as opening next month).
"""

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL   = "https://api.valr.com"
STATE_FILE = Path("/home/admin/.openclaw/workspace/bots/cm-bot-v2/scripts/recon_state.json")

MAIN_API_KEY    = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
MAIN_API_SECRET = "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"

SUBACCOUNTS = {
    "CM1":  "1483472097578319872",
    "CM2":  "1483472079069155328",
    "CMS1": "1483815480334401536",
    "CMS2": "1483815498551132160",
}


def _headers(verb, path, body="", sub_id=""):
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{verb}{path}{body}{sub_id}"
    sig = hmac.new(MAIN_API_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()
    return {
        "X-VALR-API-KEY":        MAIN_API_KEY,
        "X-VALR-SIGNATURE":      sig,
        "X-VALR-TIMESTAMP":      ts,
        "X-VALR-SUB-ACCOUNT-ID": sub_id,
    }


def get_total_balance(sub_id, label):
    path = "/v1/account/balances"
    r = requests.get(BASE_URL + path, headers=_headers("GET", path, "", sub_id), timeout=15)
    r.raise_for_status()
    balances = r.json()
    total = 0.0
    for b in balances:
        currency = b.get("currency", "")
        if currency in ("USDT", "USDC"):
            total += float(b.get("total", 0) or 0)
        elif b.get("totalInReference"):
            total += float(b["totalInReference"])
    print(f"  {label}: ${total:,.2f}")
    return total


def main():
    now = datetime.now(timezone.utc)
    key = f"closing_{now.year}-{now.month:02d}"

    print(f"Fetching balances at {now.isoformat()}\n")

    totals = {}
    for label, sub_id in SUBACCOUNTS.items():
        totals[label] = get_total_balance(sub_id, label)

    grand_total = sum(totals.values())
    print(f"\nTotal: ${grand_total:,.2f}")

    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())

    state[key] = grand_total
    state["bootstrap_detail"] = {
        "timestamp": now.isoformat(),
        "balances":  totals,
    }

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
    print(f"\nSaved: {key} = ${grand_total:,.2f} → {STATE_FILE}")


if __name__ == "__main__":
    main()
