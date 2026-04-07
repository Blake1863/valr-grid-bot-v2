#!/usr/bin/env python3
"""
Fetch and normalize VALR trading pairs.
Usage: python3 scripts/valr_pairs.py
Output: docs/VALR_PAIRS.md, docs/valr_pairs.json
"""

import requests
import json
from datetime import datetime
from collections import Counter
from pathlib import Path

def get_pairs():
    """Fetch all pairs from VALR API."""
    url = "https://api.valr.com/v1/public/pairs"
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.json()

def normalize_pairs(data, pair_type):
    """Normalize API response to standard format."""
    return [
        {
            "symbol": p["symbol"],
            "base": p["baseCurrency"],
            "quote": p["quoteCurrency"],
            "type": pair_type,
            "tickSize": p.get("tickSize", "N/A"),
            "minTradeSize": p.get("minBaseAmount", "N/A"),
            "status": "ACTIVE" if p.get("active", False) else "INACTIVE",
        }
        for p in data
    ]

def generate_markdown(spot, futures):
    """Generate markdown documentation."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(spot) + len(futures)
    
    md = f"""# VALR Trading Pairs

**Last updated:** {now}  
**Source:** VALR API `/v1/public/pairs/{{type}}`  
**Total pairs:** {total} (SPOT: {len(spot)}, FUTURES: {len(futures)})

---

## SPOT Pairs

| Symbol | Base | Quote | Tick Size | Min Size | Status |
|--------|------|-------|-----------|----------|--------|
"""
    
    for p in sorted(spot, key=lambda x: x["symbol"]):
        md += f"| {p['symbol']} | {p['base']} | {p['quote']} | {p['tickSize']} | {p['minTradeSize']} | {p['status']} |\n"
    
    md += f"""
---

## FUTURES (PERP) Pairs

| Symbol | Base | Quote | Tick Size | Min Size | Status |
|--------|------|-------|-----------|----------|--------|
"""
    
    for p in sorted(futures, key=lambda x: x["symbol"]):
        md += f"| {p['symbol']} | {p['base']} | {p['quote']} | {p['tickSize']} | {p['minTradeSize']} | {p['status']} |\n"
    
    md += f"""
---

## Summary by Quote Currency

### SPOT

| Quote | Pairs |
|-------|-------|
"""
    
    spot_quotes = Counter(p["quote"] for p in spot)
    for quote, count in sorted(spot_quotes.items()):
        md += f"| {quote} | {count} |\n"
    
    md += """
### FUTURES

| Quote | Pairs |
|-------|-------|
"""
    
    futures_quotes = Counter(p["quote"] for p in futures)
    for quote, count in sorted(futures_quotes.items()):
        md += f"| {quote} | {count} |\n"
    
    return md

def main():
    workspace = Path("/home/admin/.openclaw/workspace")
    docs_dir = workspace / "docs"
    docs_dir.mkdir(exist_ok=True)
    
    print("Fetching all pairs...")
    all_pairs = get_pairs()
    print(f"  Found {len(all_pairs)} total pairs")
    
    # Filter by currencyPairType
    spot = [p for p in all_pairs if p.get("currencyPairType") == "SPOT"]
    futures = [p for p in all_pairs if p.get("currencyPairType") == "FUTURE"]
    print(f"  SPOT: {len(spot)}, FUTURES: {len(futures)}")
    
    print("Normalizing...")
    spot_norm = normalize_pairs(spot, "SPOT")
    futures_norm = normalize_pairs(futures, "FUTURES")
    
    print("Generating markdown...")
    md = generate_markdown(spot_norm, futures_norm)
    
    # Write markdown
    md_path = docs_dir / "VALR_PAIRS.md"
    with open(md_path, "w") as f:
        f.write(md)
    print(f"✅ Written to {md_path}")
    
    # Write JSON for programmatic use
    json_path = docs_dir / "valr_pairs.json"
    with open(json_path, "w") as f:
        json.dump({"spot": spot_norm, "futures": futures_norm}, f, indent=2)
    print(f"✅ Written to {json_path}")
    
    # Print summary
    print(f"\n📊 Summary:")
    print(f"  Total pairs: {len(spot_norm) + len(futures_norm)}")
    print(f"  SPOT: {len(spot_norm)}")
    print(f"  FUTURES: {len(futures_norm)}")
    
    spot_quotes = Counter(p["quote"] for p in spot_norm)
    print(f"\n  SPOT by quote: {dict(spot_quotes)}")
    
    futures_quotes = Counter(p["quote"] for p in futures_norm)
    print(f"  FUTURES by quote: {dict(futures_quotes)}")

if __name__ == "__main__":
    main()
