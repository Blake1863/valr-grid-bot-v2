#!/usr/bin/env python3
"""
Pair Registry - Fetches all active orderbook pairs from VALR (spot + futures)
"""
import requests
import json
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime

@dataclass
class PairInfo:
    symbol: str
    type: str  # 'spot' or 'futures'
    min_qty: float
    qty_precision: int
    price_precision: int
    active: bool

class PairRegistry:
    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://api.valr.com/v1"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.pairs: Dict[str, PairInfo] = {}
    
    def _get_headers(self) -> Dict[str, str]:
        return {
            "X-VALR-API-KEY": self.api_key,
            "Content-Type": "application/json"
        }
    
    def fetch_all_pairs_single(self, orderbook_only: bool = False) -> List[PairInfo]:
        """Fetch all pairs from single endpoint and filter by type"""
        try:
            # VALR endpoint: /v1/public/pairs (returns all pairs)
            response = requests.get(
                f"{self.base_url}/public/pairs",
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            
            pairs = []
            for pair in data:
                if pair.get("active", False):
                    pair_type = "futures" if pair.get("currencyPairType") == "FUTURE" else "spot"
                    
                    # Get precision from fields
                    qty_precision = int(pair.get("baseDecimalPlaces", 8))
                    price_precision = len(pair.get("tickSize", "0.00000001").split('.')[-1]) if '.' in str(pair.get("tickSize", "0.00000001")) else 0
                    
                    pairs.append(PairInfo(
                        symbol=pair["symbol"],
                        type=pair_type,
                        min_qty=float(pair.get("minBaseAmount", 0.0001)),
                        qty_precision=qty_precision,
                        price_precision=price_precision,
                        active=True
                    ))
            
            return pairs
        except Exception as e:
            print(f"[ERROR] Fetching all pairs: {e}")
            return []
    
    def check_orderbook_active(self, symbol: str) -> bool:
        """Check if a pair has active limit orders (orderbook depth)"""
        try:
            response = requests.get(
                f"{self.base_url}/public/{symbol}/book",
                timeout=5
            )
            if response.status_code != 200:
                return False
            
            data = response.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            
            # Consider active if there are at least 2 levels on each side
            return len(bids) >= 2 and len(asks) >= 2
        except Exception:
            return False
    
    def fetch_futures_pairs(self) -> List[PairInfo]:
        """Fetch all active perpetual futures pairs"""
        try:
            # VALR endpoint: /v1/public/pairs/futures (lowercase)
            response = requests.get(
                f"{self.base_url}/public/pairs/futures",
                headers=self._get_headers(),
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            
            pairs = []
            for pair in data:
                if pair.get("active", False):
                    pairs.append(PairInfo(
                        symbol=pair["symbol"],
                        type="futures",
                        min_qty=float(pair.get("minOrderSize", 0.001)),
                        qty_precision=pair.get("quantityPrecision", 6),
                        price_precision=pair.get("pricePrecision", 2),
                        active=True
                    ))
            
            return pairs
        except Exception as e:
            print(f"[ERROR] Fetching futures pairs: {e}")
            return []
    
    def fetch_all_pairs(self, orderbook_only: bool = False) -> Dict[str, PairInfo]:
        """Fetch all active pairs (spot + futures), optionally filtering by orderbook activity"""
        print(f"[{datetime.now().isoformat()}] Fetching all pairs from VALR...")
        
        all_pairs_list = self.fetch_all_pairs_single()
        
        all_pairs = {}
        checked_count = 0
        
        for pair in all_pairs_list:
            if orderbook_only:
                # Check if pair has active orderbook
                if self.check_orderbook_active(pair.symbol):
                    all_pairs[pair.symbol] = pair
                    checked_count += 1
                    if checked_count % 50 == 0:
                        print(f"  Checked {checked_count}/{len(all_pairs_list)} pairs...")
                else:
                    continue
            else:
                all_pairs[pair.symbol] = pair
        
        self.pairs = all_pairs
        
        spot_count = len([p for p in all_pairs.values() if p.type == "spot"])
        futures_count = len([p for p in all_pairs.values() if p.type == "futures"])
        
        if orderbook_only:
            print(f"[{datetime.now().isoformat()}] Found {len(all_pairs)} pairs with active orderbooks ({spot_count} spot, {futures_count} futures)")
        else:
            print(f"[{datetime.now().isoformat()}] Found {len(all_pairs)} active pairs ({spot_count} spot, {futures_count} futures)")
        
        return all_pairs
    
    def get_pair(self, symbol: str) -> Optional[PairInfo]:
        """Get info for a specific pair"""
        return self.pairs.get(symbol)
    
    def get_all_symbols(self) -> List[str]:
        """Get list of all pair symbols"""
        return list(self.pairs.keys())

if __name__ == "__main__":
    # Test
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    
    # Load keys from env
    from dotenv import load_dotenv
    load_dotenv("/home/admin/.openclaw/secrets/cm_secrets.env")
    
    api_key = os.getenv("CM1_API_KEY")
    api_secret = os.getenv("CM1_API_SECRET")
    
    registry = PairRegistry(api_key, api_secret)
    pairs = registry.fetch_all_pairs()
    
    print("\nSample pairs:")
    for i, (symbol, info) in enumerate(list(pairs.items())[:10]):
        print(f"  {symbol}: {info.type}, min_qty={info.min_qty}")
