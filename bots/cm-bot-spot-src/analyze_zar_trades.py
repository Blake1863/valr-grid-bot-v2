#!/usr/bin/env python3
"""
Analyze ZAR pair trades for CMS1 and CMS2 to check for self-matching vs external fills.
"""

import hmac
import hashlib
import time
import requests
from datetime import datetime
from collections import defaultdict

# Credentials from .env
MAIN_API_KEY = "eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362"
MAIN_API_SECRET = "9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db"
CM1_SUBACCOUNT_ID = "1483815480334401536"
CM2_SUBACCOUNT_ID = "1483815498551132160"

ZAR_PAIRS = ["LINKZAR", "BTCZAR", "ETHZAR", "XRPZAR", "SOLZAR", "AVAXZAR", "BNBZAR"]

BASE_URL = "https://api.valr.com"

def generate_signature(secret, timestamp_ms, method, path, body, subaccount_id):
    """Generate HMAC-SHA512 signature for VALR API."""
    message = f"{timestamp_ms}{method}{path}{body}{subaccount_id}"
    signature = hmac.new(
        secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha512
    ).hexdigest()
    return signature

def parse_traded_at_to_ms(traded_at_str):
    """Convert ISO 8601 timestamp to milliseconds since epoch."""
    # Format: 2026-03-18T19:46:06.743Z
    dt = datetime.fromisoformat(traded_at_str.replace('Z', '+00:00'))
    return int(dt.timestamp() * 1000)

def fetch_trade_history(subaccount_id, limit=100):
    """Fetch trade history for a subaccount."""
    timestamp_ms = str(int(time.time() * 1000))
    method = "GET"
    path = "/v1/account/tradehistory"
    query_params = f"?limit={limit}"
    body = ""
    
    signature_path = path + query_params
    signature = generate_signature(MAIN_API_SECRET, timestamp_ms, method, signature_path, body, subaccount_id)
    
    headers = {
        "X-VALR-API-KEY": MAIN_API_KEY,
        "X-VALR-SIGNATURE": signature,
        "X-VALR-TIMESTAMP": timestamp_ms,
        "X-VALR-SUB-ACCOUNT-ID": subaccount_id
    }
    
    url = f"{BASE_URL}{path}{query_params}"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

def fetch_balance(subaccount_id):
    """Fetch ZAR balance for a subaccount."""
    timestamp_ms = str(int(time.time() * 1000))
    method = "GET"
    path = "/v1/account/balances"
    body = ""
    
    signature = generate_signature(MAIN_API_SECRET, timestamp_ms, method, path, body, subaccount_id)
    
    headers = {
        "X-VALR-API-KEY": MAIN_API_KEY,
        "X-VALR-SIGNATURE": signature,
        "X-VALR-TIMESTAMP": timestamp_ms,
        "X-VALR-SUB-ACCOUNT-ID": subaccount_id
    }
    
    url = f"{BASE_URL}{path}"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

def filter_zar_trades(trades, zar_pairs):
    """Filter trades to only include ZAR pairs."""
    return [t for t in trades if t.get('currencyPair') in zar_pairs]

def analyze_matching(cms1_trades, cms2_trades, pair_name):
    """Analyze if trades are self-matching or external fills."""
    # Group trades by timestamp (in ms)
    cms1_by_ts = {}
    for t in cms1_trades:
        if t['currencyPair'] == pair_name:
            ts_ms = parse_traded_at_to_ms(t['tradedAt'])
            t['timestamp_ms'] = ts_ms
            cms1_by_ts[ts_ms] = t
    
    cms2_by_ts = {}
    for t in cms2_trades:
        if t['currencyPair'] == pair_name:
            ts_ms = parse_traded_at_to_ms(t['tradedAt'])
            t['timestamp_ms'] = ts_ms
            cms2_by_ts[ts_ms] = t
    
    all_timestamps = set(cms1_by_ts.keys()) | set(cms2_by_ts.keys())
    
    mirrored = 0  # Opposite sides at same timestamp
    same_side = 0  # Same side at same timestamp
    one_sided = 0  # Only one subaccount traded
    
    matched_trades = []
    
    for ts in sorted(all_timestamps, reverse=True):
        cms1_trade = cms1_by_ts.get(ts)
        cms2_trade = cms2_by_ts.get(ts)
        
        if cms1_trade and cms2_trade:
            # Both traded at same timestamp
            if cms1_trade['side'] != cms2_trade['side']:
                mirrored += 1
                matched_trades.append({
                    'timestamp': ts,
                    'timestamp_str': cms1_trade['tradedAt'],
                    'pair': pair_name,
                    'cms1_side': cms1_trade['side'],
                    'cms2_side': cms2_trade['side'],
                    'cms1_qty': cms1_trade.get('quantity', 'N/A'),
                    'cms2_qty': cms2_trade.get('quantity', 'N/A'),
                    'cms1_price': cms1_trade.get('price', 'N/A'),
                    'cms2_price': cms2_trade.get('price', 'N/A'),
                })
            else:
                same_side += 1
                matched_trades.append({
                    'timestamp': ts,
                    'timestamp_str': cms1_trade['tradedAt'],
                    'pair': pair_name,
                    'cms1_side': cms1_trade['side'],
                    'cms2_side': cms2_trade['side'],
                    'cms1_qty': cms1_trade.get('quantity', 'N/A'),
                    'cms2_qty': cms2_trade.get('quantity', 'N/A'),
                    'issue': 'SAME_SIDE'
                })
        else:
            # Only one subaccount traded
            one_sided += 1
            trader = "CMS1" if cms1_trade else "CMS2"
            trade = cms1_trade or cms2_trade
            matched_trades.append({
                'timestamp': ts,
                'timestamp_str': trade['tradedAt'],
                'pair': pair_name,
                'trader': trader,
                'side': trade['side'],
                'qty': trade.get('quantity', 'N/A'),
                'price': trade.get('price', 'N/A'),
                'issue': 'ONE_SIDED'
            })
    
    total = len(all_timestamps)
    pct_self_matched = (mirrored / total * 100) if total > 0 else 0
    
    return {
        'pair': pair_name,
        'total': total,
        'mirrored': mirrored,
        'same_side': same_side,
        'one_sided': one_sided,
        'pct_self_matched': pct_self_matched,
        'trades': matched_trades
    }

def main():
    print("=" * 80)
    print("ZAR PAIR SELF-MATCHING ANALYSIS")
    print("=" * 80)
    
    # Fetch trade history for both subaccounts
    print("\nFetching trade history...")
    cms1_trades = fetch_trade_history(CM1_SUBACCOUNT_ID, limit=100)
    cms2_trades = fetch_trade_history(CM2_SUBACCOUNT_ID, limit=100)
    
    print(f"CMS1 trades fetched: {len(cms1_trades)}")
    print(f"CMS2 trades fetched: {len(cms2_trades)}")
    
    # Filter to ZAR pairs
    cms1_zar = filter_zar_trades(cms1_trades, ZAR_PAIRS)
    cms2_zar = filter_zar_trades(cms2_trades, ZAR_PAIRS)
    
    print(f"CMS1 ZAR trades: {len(cms1_zar)}")
    print(f"CMS2 ZAR trades: {len(cms2_zar)}")
    
    # Show which pairs have trades
    print("\nZAR pairs with trades:")
    cms1_pairs = set(t['currencyPair'] for t in cms1_zar)
    cms2_pairs = set(t['currencyPair'] for t in cms2_zar)
    print(f"  CMS1: {sorted(cms1_pairs)}")
    print(f"  CMS2: {sorted(cms2_pairs)}")
    
    # Analyze each pair
    print("\n" + "=" * 80)
    print("PAIR-BY-PAIR ANALYSIS")
    print("=" * 80)
    
    results = []
    for pair in ZAR_PAIRS:
        result = analyze_matching(cms1_zar, cms2_zar, pair)
        results.append(result)
        
        status = "✓ OK" if result['pct_self_matched'] == 100 else "⚠ PROBLEM"
        print(f"\n{pair}:")
        print(f"  Total trades: {result['total']}")
        print(f"  Mirrored (opposite sides): {result['mirrored']}")
        print(f"  Same side: {result['same_side']}")
        print(f"  One-sided: {result['one_sided']}")
        print(f"  % Self-matched: {result['pct_self_matched']:.1f}% {status}")
    
    # Show detailed trades for LINKZAR and XRPZAR
    print("\n" + "=" * 80)
    print("DETAILED TRADE VIEW - LINKZAR (5 most recent)")
    print("=" * 80)
    
    linkzar_result = next(r for r in results if r['pair'] == 'LINKZAR')
    for i, trade in enumerate(linkzar_result['trades'][:5]):
        print(f"\n{i+1}. Timestamp: {trade['timestamp_str']}")
        if 'issue' in trade:
            print(f"   ⚠ {trade['issue']}")
            if trade['issue'] == 'ONE_SIDED':
                print(f"   Trader: {trade['trader']}, Side: {trade['side']}, Qty: {trade['qty']}, Price: {trade['price']}")
            else:
                print(f"   CMS1: {trade['cms1_side']}, CMS2: {trade['cms2_side']}")
        else:
            print(f"   CMS1: {trade['cms1_side']} @ {trade['cms1_price']} (qty: {trade['cms1_qty']})")
            print(f"   CMS2: {trade['cms2_side']} @ {trade['cms2_price']} (qty: {trade['cms2_qty']})")
    
    print("\n" + "=" * 80)
    print("DETAILED TRADE VIEW - XRPZAR (5 most recent)")
    print("=" * 80)
    
    xrpzar_result = next(r for r in results if r['pair'] == 'XRPZAR')
    for i, trade in enumerate(xrpzar_result['trades'][:5]):
        print(f"\n{i+1}. Timestamp: {trade['timestamp_str']}")
        if 'issue' in trade:
            print(f"   ⚠ {trade['issue']}")
            if trade['issue'] == 'ONE_SIDED':
                print(f"   Trader: {trade['trader']}, Side: {trade['side']}, Qty: {trade['qty']}, Price: {trade['price']}")
            else:
                print(f"   CMS1: {trade['cms1_side']}, CMS2: {trade['cms2_side']}")
        else:
            print(f"   CMS1: {trade['cms1_side']} @ {trade['cms1_price']} (qty: {trade['cms1_qty']})")
            print(f"   CMS2: {trade['cms2_side']} @ {trade['cms2_price']} (qty: {trade['cms2_qty']})")
    
    # Check ZAR balances
    print("\n" + "=" * 80)
    print("ZAR BALANCE CHECK")
    print("=" * 80)
    
    cms1_balance = fetch_balance(CM1_SUBACCOUNT_ID)
    cms2_balance = fetch_balance(CM2_SUBACCOUNT_ID)
    
    cms1_zar_balance = next((b for b in cms1_balance if b.get('currency') == 'ZAR'), None)
    cms2_zar_balance = next((b for b in cms2_balance if b.get('currency') == 'ZAR'), None)
    
    print(f"\nCMS1 ZAR Balance: {cms1_zar_balance['available'] if cms1_zar_balance else 'N/A'}")
    print(f"CMS2 ZAR Balance: {cms2_zar_balance['available'] if cms2_zar_balance else 'N/A'}")
    
    # Verdict
    print("\n" + "=" * 80)
    print("VERDICT")
    print("=" * 80)
    
    problem_pairs = [r for r in results if r['pct_self_matched'] < 100]
    
    if not problem_pairs:
        print("\n✓ ALL ZAR PAIRS ARE SELF-MATCHING CORRECTLY")
        print("  All trades show opposite sides at matching timestamps.")
    else:
        print(f"\n⚠ PROBLEM DETECTED IN {len(problem_pairs)} PAIR(S):")
        for r in problem_pairs:
            print(f"  - {r['pair']}: {r['pct_self_matched']:.1f}% self-matched")
            print(f"    Same-side: {r['same_side']}, One-sided: {r['one_sided']}")
        
        print("\n  This indicates:")
        print("  - Same-side trades: Both subaccounts trading same direction (not self-matching)")
        print("  - One-sided trades: Only one subaccount filling (external takers hitting the book)")
    
    print("\n" + "=" * 80)

if __name__ == "__main__":
    main()
