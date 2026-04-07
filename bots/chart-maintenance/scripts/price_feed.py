#!/usr/bin/env python3
"""
Price Feed - WebSocket-based real-time price feed for chart maintenance
Subscribes to OB_L1_DIFF streams for all active pairs
"""
import websocket
import json
import threading
import time
from typing import Dict, Optional
from datetime import datetime

class PriceFeed:
    def __init__(self, ws_url: str = "wss://api.valr.com/ws/trade"):
        self.ws_url = ws_url
        self.ws = None
        self.prices: Dict[str, float] = {}  # symbol -> mid_price
        self.connected = False
        self.subscribed_pairs = set()
        self._ws_thread = None
        self._last_message_time = 0
    
    def connect(self, timeout_seconds: float = 10.0):
        """Connect to WebSocket and start listener thread"""
        if self._ws_thread and self._ws_thread.is_alive():
            print("[WARN] WebSocket already connected")
            return True
        
        print(f"[{datetime.now().isoformat()}] Connecting price feed WS: {self.ws_url}")
        self._ws_thread = threading.Thread(target=self._run_ws, daemon=True)
        self._ws_thread.start()
        
        # Wait for connection
        start = time.time()
        while time.time() - start < timeout_seconds:
            if self.connected:
                print(f"[{datetime.now().isoformat()}] Price feed WS connected ✅")
                return True
            time.sleep(0.1)
        
        print("[ERROR] Price feed WS connection timeout")
        return False
    
    def wait_for_prices(self, symbols: list, timeout_seconds: float = 10.0):
        """Wait until we have prices for all symbols or timeout"""
        start = time.time()
        while time.time() - start < timeout_seconds:
            all_have_prices = all(s in self.prices for s in symbols)
            if all_have_prices:
                return True
            time.sleep(0.2)
        return False
    
    def _run_ws(self):
        """WebSocket runner thread"""
        try:
            self.ws = websocket.WebSocketApp(
                self.ws_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close
            )
            self.ws.run_forever()
        except Exception as e:
            print(f"[ERROR] Price feed WS exception: {e}")
            self.connected = False
    
    def _on_open(self, ws):
        """Called when WS connection opens"""
        print(f"[{datetime.now().isoformat()}] Price feed WS opened")
        self.connected = True
        
        # Resubscribe to all pairs if reconnecting
        if self.subscribed_pairs:
            for symbol in self.subscribed_pairs:
                self._subscribe(symbol)
    
    def _on_message(self, ws, message):
        """Handle incoming WS messages"""
        try:
            data = json.loads(message)
            msg_type = data.get("type")
            
            if msg_type == "OB_L1_DIFF":
                symbol = data.get("currencyPair")
                if symbol:
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    
                    if bids and asks:
                        bid = float(bids[0]["price"])
                        ask = float(asks[0]["price"])
                        mid = (bid + ask) / 2
                        self.prices[symbol] = mid
                        self._last_message_time = time.time()
                        
            elif msg_type == "SUBSCRIPTION_CONFIRMATION":
                symbol = data.get("currencyPair")
                if symbol:
                    print(f"[{datetime.now().isoformat()}] Subscribed to {symbol} OB_L1_DIFF")
                    
        except Exception as e:
            print(f"[ERROR] Parsing WS message: {e}")
    
    def _on_error(self, ws, error):
        """Handle WS errors"""
        print(f"[ERROR] Price feed WS: {error}")
    
    def _on_close(self, ws, close_status_code, close_msg):
        """Handle WS close"""
        print(f"[{datetime.now().isoformat()}] Price feed WS closed: {close_status_code} {close_msg}")
        self.connected = False
    
    def _subscribe(self, symbol: str):
        """Subscribe to orderbook updates for a symbol"""
        if not self.connected or not self.ws:
            return
        
        sub_msg = {
            "type": "SUBSCRIBE",
            "currencyPair": symbol,
            "messageTypes": ["OB_L1_DIFF"]
        }
        
        try:
            self.ws.send(json.dumps(sub_msg))
            self.subscribed_pairs.add(symbol)
        except Exception as e:
            print(f"[ERROR] Subscribing to {symbol}: {e}")
    
    def subscribe(self, symbol: str):
        """Subscribe to a symbol (thread-safe)"""
        if symbol in self.subscribed_pairs:
            return
        
        if self.connected:
            self._subscribe(symbol)
        else:
            # Will subscribe on connect
            self.subscribed_pairs.add(symbol)
    
    def subscribe_many(self, symbols: list):
        """Subscribe to multiple symbols"""
        for symbol in symbols:
            self.subscribe(symbol)
    
    def get_price(self, symbol: str) -> Optional[float]:
        """Get latest mid price for a symbol"""
        return self.prices.get(symbol)
    
    def is_fresh(self, symbol: str, max_age_seconds: float = 5.0) -> bool:
        """Check if price data is fresh (received within max_age_seconds)"""
        if symbol not in self.prices:
            return False
        return (time.time() - self._last_message_time) < max_age_seconds
    
    def close(self):
        """Close WebSocket connection"""
        if self.ws:
            self.ws.close()
        self.connected = False

# Global instance
_price_feed: Optional[PriceFeed] = None

def get_price_feed() -> PriceFeed:
    """Get or create global price feed instance"""
    global _price_feed
    if _price_feed is None:
        _price_feed = PriceFeed()
    return _price_feed

if __name__ == "__main__":
    # Test
    feed = get_price_feed()
    if feed.connect():
        feed.subscribe("BTCZAR")
        feed.subscribe("SOLUSDTPERP")
        
        # Print prices for 30 seconds
        for i in range(30):
            for symbol in ["BTCZAR", "SOLUSDTPERP"]:
                price = feed.get_price(symbol)
                if price:
                    print(f"{symbol}: {price:.8f}")
            time.sleep(1)
        
        feed.close()
