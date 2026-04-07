#!/usr/bin/env python3
"""
Order Executor - Places maker (post-only) and taker (IOC) orders for chart maintenance
"""
import requests
import hmac
import hashlib
import time
import json
from typing import Dict, Optional, Tuple
from datetime import datetime
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from price_feed import get_price_feed

class OrderExecutor:
    def __init__(self, cm1_key: str, cm1_secret: str, cm2_key: str, cm2_secret: str,
                 base_url: str = "https://api.valr.com/v1",
                 subaccount_id: str = ""):
        self.accounts = {
            "CM1": {"key": cm1_key, "secret": cm1_secret},
            "CM2": {"key": cm2_key, "secret": cm2_secret}
        }
        self.base_url = base_url
        self.subaccount_id = subaccount_id  # For subaccount API calls
    
    def _get_timestamp(self) -> str:
        return str(int(time.time() * 1000))
    
    def _get_headers(self, account: str, method: str, path: str, body: str = "") -> Dict[str, str]:
        """Get authenticated headers for a specific account"""
        acc = self.accounts[account]
        timestamp = self._get_timestamp()
        # For signature, path must include /v1 prefix
        sig_path = path if path.startswith('/v1') else f'/v1{path}'
        # VALR uses: timestamp + method + path + body + subaccountId, SHA512
        # Secret is used as-is (string), NOT hex-decoded
        message = f"{timestamp}{method}{sig_path}{body}{self.subaccount_id}"
        signature = hmac.new(
            acc["secret"].encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha512
        ).hexdigest()
        
        headers = {
            "X-VALR-API-KEY": acc["key"],
            "X-VALR-SIGNATURE": signature,
            "X-VALR-TIMESTAMP": timestamp,
            "Content-Type": "application/json"
        }
        
        # Add subaccount header if using subaccount
        if self.subaccount_id:
            headers["X-VALR-SUB-ACCOUNT-ID"] = self.subaccount_id
        
        return headers
    
    def get_mid_price(self, symbol: str) -> Optional[float]:
        """Get mid price from orderbook (authenticated)"""
        try:
            acc = self.accounts["CM1"]
            timestamp = str(int(time.time() * 1000))
            sig_path = f"/v1/orderbook"
            body = f"currencyPair={symbol}"
            message = f"{timestamp}GET{sig_path}?{body}"
            signature = hmac.new(
                acc["secret"].encode('utf-8'),
                message.encode('utf-8'),
                hashlib.sha512
            ).hexdigest()
            
            headers = {
                "X-VALR-API-KEY": acc["key"],
                "X-VALR-SIGNATURE": signature,
                "X-VALR-TIMESTAMP": timestamp
            }
            
            response = requests.get(f"{self.base_url}/orderbook?currencyPair={symbol}", headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                bids = data.get("buyOrders", [])
                asks = data.get("sellOrders", [])
                if bids and asks:
                    bid = float(bids[0]["price"])
                    ask = float(asks[0]["price"])
                    return (bid + ask) / 2
        except Exception as e:
            pass  # Fall through to None
        return None
    
    def get_mark_price(self, symbol: str, pair_type: str = "futures") -> Optional[float]:
        """Get average of mid + mark price for a pair
        
        Uses average of orderbook mid and mark price to reduce external fill risk.
        
        Priority:
        1. WebSocket price feed (real-time, low latency) - used as mid
        2. REST API mark price endpoint
        3. Orderbook mid (authenticated)
        4. Default price (last resort)
        """
        # Get mark price from REST
        mark_price = None
        try:
            path = f"/public/{symbol}/markprice"
            response = requests.get(f"{self.base_url}{path}", timeout=10)
            if response.status_code == 200:
                data = response.json()
                mark_price = float(data.get("markPrice", 0))
                if mark_price <= 0:
                    mark_price = None
        except Exception:
            pass
        
        # Get mid price from WebSocket or orderbook
        mid_price = None
        
        # Try WebSocket first
        try:
            feed = get_price_feed()
            ws_price = feed.get_price(symbol)
            if ws_price and feed.is_fresh(symbol, max_age_seconds=5.0):
                mid_price = ws_price
        except Exception:
            pass
        
        # Fallback to orderbook REST
        if mid_price is None:
            mid_price = self.get_mid_price(symbol)
        
        # Return average of mid + mark, or whichever is available
        if mid_price and mark_price:
            return (mid_price + mark_price) / 2
        elif mid_price:
            return mid_price
        elif mark_price:
            return mark_price
        else:
            return self._get_default_price(symbol)
    
    def _get_spot_price_from_orderbook(self, symbol: str) -> Optional[float]:
        """Get spot price from orderbook mid (authenticated)"""
        try:
            # Use the first account's credentials
            acc = self.accounts["CM1"]
            timestamp = str(int(time.time() * 1000))
            sig_path = f"/v1/orderbook"
            body = f"currencyPair={symbol}"
            message = f"{timestamp}GET{sig_path}?{body}"
            signature = hmac.new(
                acc["secret"].encode('utf-8'),
                message.encode('utf-8'),
                hashlib.sha512
            ).hexdigest()
            
            headers = {
                "X-VALR-API-KEY": acc["key"],
                "X-VALR-SIGNATURE": signature,
                "X-VALR-TIMESTAMP": timestamp
            }
            
            response = requests.get(f"{self.base_url}/orderbook?currencyPair={symbol}", headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                bids = data.get("buyOrders", [])
                asks = data.get("sellOrders", [])
                if bids and asks:
                    bid = float(bids[0]["price"])
                    ask = float(asks[0]["price"])
                    return (bid + ask) / 2
            # Fallback: use reasonable default prices for common pairs
            return self._get_default_price(symbol)
        except Exception as e:
            print(f"[ERROR] Getting orderbook for {symbol}: {e}")
            # Last resort: use default price
            return self._get_default_price(symbol)
    
    def _get_default_price(self, symbol: str) -> Optional[float]:
        """Get default/fallback price for common pairs"""
        # Approximate prices (will be refined by actual trades)
        defaults = {
            "BTCZAR": 1500000.0,  # ~1.5M ZAR
            "ETHZAR": 45000.0,    # ~45k ZAR
            "BTCUSDT": 74000.0,   # ~74k USDT
            "ETHUSDT": 2300.0,    # ~2.3k USDT
            "SOLUSDT": 94.0,      # ~94 USDT
        }
        price = defaults.get(symbol)
        if price:
            print(f"[WARN] Using default price for {symbol}: {price}")
        return price
    
    def place_order(self, account: str, symbol: str, side: str, 
                    quantity: float, price: float, 
                    post_only: bool = False, ioc: bool = False,
                    reprice: bool = False,
                    price_precision: int = 2,
                    qty_precision: int = 8) -> dict:
        """
        Place an order on VALR using /v1/orders/limit endpoint
        
        Args:
            account: CM1 or CM2
            symbol: Trading pair symbol (e.g., SOLUSDTPERP)
            side: BUY or SELL
            quantity: Order quantity
            price: Limit price
            post_only: If True, order will be rejected if it would execute immediately
            ioc: If True, immediate-or-cancel (fill what you can, cancel rest)
            reprice: If True with post_only, reprice instead of reject
            price_precision: Price precision for rounding (default 2 for SOLUSDTPERP)
            qty_precision: Quantity precision for formatting (default 8 for BTC)
        """
        try:
            # VALR endpoint: POST /v1/orders/limit
            path = "/orders/limit"
            
            # Round price to correct precision
            rounded_price = round(price, price_precision)
            
            # Build order body - VALR expects 'pair' field
            # Format quantity and price without scientific notation
            qty_str = f"{quantity:.{qty_precision}f}"
            price_str = f"{rounded_price:.{price_precision}f}"
            
            body_dict = {
                "pair": symbol,
                "side": side.upper(),
                "type": "Limit",
                "quantity": qty_str,
                "price": price_str
            }
            
            # Handle post-only and IOC
            if post_only:
                body_dict["postOnly"] = True
                if reprice:
                    body_dict["reprice"] = True
            
            if ioc:
                body_dict["timeInForce"] = "IOC"
            else:
                body_dict["timeInForce"] = "GTC"
            
            body = json.dumps(body_dict)
            headers = self._get_headers(account, "POST", path, body)
            
            response = requests.post(f"{self.base_url}{path}", headers=headers, json=body_dict, timeout=10)
            
            # 200 = OK (order placed), 202 = Accepted (order placed, async processing)
            # Both are success cases
            if response.status_code in [200, 202]:
                result = response.json()
                # VALR returns 'id' not 'orderId' - normalize it
                order_id = result.get('id') or result.get('orderId')
                result['orderId'] = order_id  # Add normalized key
                print(f"[{datetime.now().isoformat()}] Order placed: {account} {side} {qty_str} {symbol} @ {price} (post_only={post_only}, ioc={ioc})")
                print(f"   → Order ID: {order_id}")
                return {"success": True, "data": result}
            else:
                try:
                    error_data = response.json()
                    error_text = error_data.get('message', response.text)
                except:
                    error_text = response.text
                print(f"[ERROR] Order failed for {account} {symbol} {side}: {error_text}")
                return {"success": False, "error": error_text, "status_code": response.status_code}
            
        except Exception as e:
            print(f"[ERROR] Placing order for {account} {symbol}: {e}")
            return {"success": False, "error": str(e)}
    
    def cancel_order(self, account: str, symbol: str, order_id: str) -> dict:
        """Cancel a specific order"""
        try:
            path = f"/orders/{symbol}/{order_id}"
            headers = self._get_headers(account, "DELETE", path)
            
            response = requests.delete(f"{self.base_url}{path}", headers=headers, timeout=10)
            
            if response.status_code != 200:
                return {"success": False, "error": response.text}
            
            return {"success": True, "data": response.json()}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def cancel_all_orders(self, account: str, symbol: str) -> dict:
        """Cancel all orders for a symbol"""
        try:
            path = f"/orders/{symbol}"
            headers = self._get_headers(account, "DELETE", path)
            
            response = requests.delete(f"{self.base_url}{path}", headers=headers, timeout=10)
            
            if response.status_code != 200:
                return {"success": False, "error": response.text}
            
            return {"success": True, "data": response.json()}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def get_open_orders(self, account: str, symbol: str = None) -> list:
        """Get open orders for an account
        
        Args:
            account: CM1 or CM2
            symbol: Optional symbol filter (returns all if None)
        """
        try:
            # VALR endpoint: GET /v1/orders/open
            path = "/orders/open"
            
            headers = self._get_headers(account, "GET", path)
            response = requests.get(f"{self.base_url}{path}", headers=headers, timeout=10)
            response.raise_for_status()
            
            all_orders = response.json()
            
            # Filter by symbol if requested
            if symbol and all_orders:
                return [o for o in all_orders if o.get("currencyPair") == symbol]
            
            return all_orders
        except Exception as e:
            # Silently fail - returns empty list
            return []
    
    def execute_cycle(self, pair_symbol: str, pair_info, maker_account: str, 
                      taker_account: str, qty: float, maker_side: str = "BUY") -> dict:
        """
        Execute a full chart maintenance cycle:
        1. Maker places LIMIT order (GTC) to rest on book
        2. Taker places LIMIT + IOC to hit maker's resting order
        
        IMPORTANT: For wash trades to work, use SAME account for both sides
        to avoid VALR's cross-account self-trade prevention.
        
        Args:
            maker_side: "BUY" or "SELL" - determines which side maker takes
        
        Returns result of the cycle
        """
        # Get mark price - use this for BOTH orders
        mark_price = self.get_mark_price(pair_symbol, pair_info.type)
        if not mark_price:
            return {"success": False, "error": "Failed to get mark price"}
        
        # Round to pair's precision
        price = round(mark_price, pair_info.price_precision)
        
        # Use passed maker_side (from cycle tracker for role rotation)
        taker_side = "SELL" if maker_side == "BUY" else "BUY"
        
        # Both orders at SAME price - they will match
        maker_price = price
        taker_price = price
        
        print(f"[{datetime.now().isoformat()}] Starting cycle: {maker_account} (maker) {maker_side} vs {taker_account} (taker) {taker_side}")
        print(f"  Symbol: {pair_symbol}, Qty: {qty}")
        print(f"  Mark price (mid+mark avg): {price:,}")
        print(f"  Maker: {maker_price:,} (LIMIT GTC)")
        print(f"  Taker: {taker_price:,} (LIMIT IOC)")
        
        # Step 1: Maker places LIMIT order (GTC) to rest on book
        # Step 2: Wait 5ms
        # Step 3: Taker places LIMIT + IOC to hit maker's resting order
        # Taker must be SLIGHTLY aggressive to ensure it hits OUR maker order
        
        # Maker order: LIMIT + GTC (rests on book)
        maker_result = self.place_order(
            account=maker_account,
            symbol=pair_symbol,
            side=maker_side,
            quantity=qty,
            price=maker_price,
            ioc=False,  # GTC - rests on book
            price_precision=pair_info.price_precision,
            qty_precision=pair_info.qty_precision
        )
        
        if not maker_result["success"]:
            return {"success": False, "error": f"Maker order failed: {maker_result.get('error')}"}
        
        maker_order_id = maker_result["data"].get("orderId")
        
        # Wait 5ms for maker to reach book before taker fires
        time.sleep(0.005)  # 5ms delay
        
        # Taker must be SLIGHTLY aggressive to hit our maker (0.01% better)
        if taker_side == "BUY":
            aggressive_price = round(taker_price * 1.0001, pair_info.price_precision)
        else:  # SELL
            aggressive_price = round(taker_price * 0.9999, pair_info.price_precision)
        
        # Step 2: Taker places LIMIT + IOC order (slightly aggressive to hit maker)
        taker_result = self.place_order(
            account=taker_account,
            symbol=pair_symbol,
            side=taker_side,
            quantity=qty,
            price=aggressive_price,
            ioc=True,  # IOC - fills immediately or cancels
            price_precision=pair_info.price_precision,
            qty_precision=pair_info.qty_precision
        )
        
        if not taker_result["success"]:
            # Taker order failed - cancel maker order
            if maker_order_id:
                self.cancel_order(maker_account, pair_symbol, maker_order_id)
            return {"success": False, "error": f"Taker order failed: {taker_result.get('error')}"}
        
        taker_order_id = taker_result["data"].get("orderId")
        
        # Step 4: Wait for fills and verify
        time.sleep(0.5)
        
        # Check if orders are still open
        maker_open = self.get_open_orders(maker_account, pair_symbol)
        taker_open = self.get_open_orders(taker_account, pair_symbol)
        
        maker_still_open = [o for o in maker_open if o.get("orderId") == maker_order_id]
        taker_still_open = [o for o in taker_open if o.get("orderId") == taker_order_id]
        
        # Determine fill status
        maker_filled = len(maker_still_open) == 0
        taker_filled = len(taker_still_open) == 0
        
        cycle_result = {
            "success": True,
            "pair": pair_symbol,
            "maker_account": maker_account,
            "taker_account": taker_account,
            "maker_side": maker_side,
            "taker_side": taker_side,
            "quantity": qty,
            "price": price,
            "maker_order_id": maker_order_id,
            "taker_order_id": taker_order_id,
            "maker_filled": maker_filled,
            "taker_filled": taker_filled,
            "timestamp": datetime.now().isoformat()
        }
        
        if maker_filled and taker_filled:
            print(f"[{datetime.now().isoformat()}] ✅ Cycle COMPLETE: Both orders filled")
        elif maker_filled:
            print(f"[{datetime.now().isoformat()}] ⚠️  Maker filled, taker still open")
            cycle_result["partial_fill"] = True
        elif taker_filled:
            print(f"[{datetime.now().isoformat()}] ⚠️  Taker filled, maker still open")
            cycle_result["partial_fill"] = True
        else:
            print(f"[{datetime.now().isoformat()}] ❌ Cycle INCOMPLETE: Orders still open")
            print(f"   Maker order {maker_order_id}: {maker_still_open[0].get('status') if maker_still_open else 'not found'}")
            print(f"   Taker order {taker_order_id}: {taker_still_open[0].get('status') if taker_still_open else 'not found'}")
            cycle_result["no_fill"] = True
        
        return cycle_result

if __name__ == "__main__":
    # Test
    import os
    from dotenv import load_dotenv
    load_dotenv("/home/admin/.openclaw/secrets/cm_secrets.env")
    
    cm1_key = os.getenv("CM1_API_KEY")
    cm1_secret = os.getenv("CM1_API_SECRET")
    cm2_key = os.getenv("CM2_API_KEY")
    cm2_secret = os.getenv("CM2_API_SECRET")
    
    executor = OrderExecutor(cm1_key, cm1_secret, cm2_key, cm2_secret)
    
    # Test mark price
    price = executor.get_mark_price("SOLUSDTPERP", "futures")
    print(f"SOLUSDTPERP mark price: {price}")
