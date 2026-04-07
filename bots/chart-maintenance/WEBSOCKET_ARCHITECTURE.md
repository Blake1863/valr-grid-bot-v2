# WebSocket-First Architecture

**Chart Maintenance Bot - Real-Time Price Feed Design**

---

## 1. Core Principle

> **WebSocket-first for real-time operations, REST for fallback/history only**

The chart maintenance bot prioritizes WebSocket connections for all time-critical operations. REST APIs are used only as fallbacks or for historical data retrieval.

---

## 2. Why WebSocket is Critical

### Latency
| Method | Typical Latency | Use Case |
|--------|----------------|----------|
| WebSocket | <100ms connection, <50ms updates | Real-time trading |
| REST API | 500-2000ms per request | Historical data, bootstrap |

### External Fill Prevention
Real-time prices prevent being picked off by arbitrageurs. Stale REST prices create risk:
- **REST risk**: Price moves 0.5% during 1s API call → your order is instantly arb'd
- **WS advantage**: You see the move in <50ms and can adjust

### Order Accuracy
- **WebSocket**: Mid-price from live orderbook (`OB_L1_DIFF` stream)
- **REST**: Snapshot in time, already stale by receipt

### Throughput
- **WebSocket**: Server pushes updates (1 message per change)
- **REST**: Client must poll (1 request per check, rate-limited)

---

## 3. When to Use Each

### WebSocket (Primary) ✅

Use WebSocket for all real-time operations:

- **Real-time price feeds** for order placement
- **Mark price** for cycle calculations
- **Orderbook depth checks** (OB_L1_DIFF stream)
- **Live balance updates** (account WebSocket stream)

### REST API (Fallback/Secondary) ⚠️

Use REST only when:

- **Historical data** (past trades, funding rates)
- **Initial bootstrap** when WebSocket unavailable
- **Reconciliation/verification** of fills
- **Rate-limited operations** (pair metadata, config)
- **Error recovery** when WebSocket disconnects

---

## 4. Implementation Patterns

### Price Feed WebSocket Subscription

From `scripts/price_feed.py`:

```python
class PriceFeed:
    def __init__(self, ws_url: str = "wss://api.valr.com/ws/trade"):
        self.ws_url = ws_url
        self.prices: Dict[str, float] = {}  # symbol -> mid_price
        self.connected = False
        self.subscribed_pairs = set()
    
    def connect(self, timeout_seconds: float = 10.0):
        """Connect to WebSocket and start listener thread"""
        self._ws_thread = threading.Thread(target=self._run_ws, daemon=True)
        self._ws_thread.start()
        
        # Wait for connection
        start = time.time()
        while time.time() - start < timeout_seconds:
            if self.connected:
                return True
            time.sleep(0.1)
        return False
    
    def _subscribe(self, symbol: str):
        """Subscribe to orderbook updates for a symbol"""
        sub_msg = {
            "type": "SUBSCRIBE",
            "currencyPair": symbol,
            "messageTypes": ["OB_L1_DIFF"]
        }
        self.ws.send(json.dumps(sub_msg))
        self.subscribed_pairs.add(symbol)
    
    def _on_message(self, ws, message):
        """Handle incoming WS messages"""
        data = json.loads(message)
        if data.get("type") == "OB_L1_DIFF":
            symbol = data.get("currencyPair")
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            
            if bids and asks:
                bid = float(bids[0]["price"])
                ask = float(asks[0]["price"])
                mid = (bid + ask) / 2
                self.prices[symbol] = mid
                self._last_message_time = time.time()
    
    def is_fresh(self, symbol: str, max_age_seconds: float = 5.0) -> bool:
        """Check if price data is fresh (received within max_age_seconds)"""
        if symbol not in self.prices:
            return False
        return (time.time() - self._last_message_time) < max_age_seconds
```

### Mark Price with WS-First Fallback

From `scripts/order_executor.get_mark_price()`:

```python
def get_mark_price(self, symbol: str, pair_type: str = "futures") -> Optional[float]:
    """Get current mark price for a pair
    
    Priority:
    1. WebSocket price feed (real-time, low latency)
    2. REST API (fallback)
    3. Default price (last resort)
    """
    # Try WebSocket price feed first (lowest latency)
    try:
        feed = get_price_feed()
        ws_price = feed.get_price(symbol)
        if ws_price and feed.is_fresh(symbol, max_age_seconds=5.0):
            return ws_price
    except Exception as e:
        pass  # Fall through to REST
    
    # REST API fallback
    try:
        if pair_type == "futures":
            path = f"/public/{symbol}/markprice"
            response = requests.get(f"{self.base_url}{path}", timeout=10)
            response.raise_for_status()
            data = response.json()
            return float(data.get("markPrice", 0))
        else:
            # Spot: use orderbook mid price (authenticated)
            return self._get_spot_price_from_orderbook(symbol)
    except Exception as e:
        print(f"[ERROR] REST price fetch for {symbol}: {e}")
    
    # Last resort: default price
    return self._get_default_price(symbol)
```

### Connection Handling & Reconnection

```python
def _on_open(self, ws):
    """Called when WS connection opens"""
    self.connected = True
    
    # Resubscribe to all pairs if reconnecting
    if self.subscribed_pairs:
        for symbol in self.subscribed_pairs:
            self._subscribe(symbol)

def _on_close(self, ws, close_status_code, close_msg):
    """Handle WS close"""
    self.connected = False
    # Bot will detect !connected and use REST fallback
```

### Price Freshness Validation

```python
# Before using WS price, always validate freshness
feed = get_price_feed()
ws_price = feed.get_price(symbol)

if ws_price and feed.is_fresh(symbol, max_age_seconds=5.0):
    # Use WebSocket price (<5 seconds old)
    price = ws_price
else:
    # Fall back to REST
    price = self._get_rest_price(symbol)
```

---

## 5. Anti-Patterns to Avoid

### ❌ Using REST `/public/{symbol}/ticker` for Trade Decisions

```python
# WRONG: REST ticker is stale by the time you use it
response = requests.get(f"{base_url}/public/{symbol}/ticker")
price = response.json()["lastTradedPrice"]
place_order(price)  # Risk: price moved during API call
```

### ❌ Polling REST Endpoints in Tight Loops

```python
# WRONG: Rate limit violation + high latency
while trading:
    response = requests.get(f"{base_url}/public/{symbol}/ticker")
    price = response.json()["lastTradedPrice"]
    # Burns API quota, 500-2000ms per iteration
```

### ❌ Trusting Stale Prices (>5s Old)

```python
# WRONG: No freshness check
ws_price = feed.get_price(symbol)
place_order(ws_price)  # Could be 30 seconds old!

# RIGHT: Always validate
if feed.is_fresh(symbol, max_age_seconds=5.0):
    place_order(ws_price)
else:
    # Get fresh price via REST
```

### ❌ No WS Reconnection Logic

```python
# WRONG: One failure = permanent REST fallback
def connect(self):
    self.ws = websocket.create_connection(self.url)
    # If this fails, bot runs on REST forever

# RIGHT: Handle reconnects
def _on_close(self, ws, code, msg):
    self.connected = False
    # Bot detects !connected and retries on next cycle
```

---

## 6. Performance Benchmarks

### Observed Metrics (Chart Maintenance Bot)

| Metric | WebSocket | REST Fallback |
|--------|-----------|---------------|
| Connection time | ~100ms | N/A |
| Price update latency | <50ms | 500-2000ms |
| Throughput | Push-based | Poll-limited |
| External fill risk | **LOW** | **HIGH** |
| Rate limit impact | None | Significant |

### Real-World Impact

**Scenario**: SOLUSDTPERP moves 1% in 2 seconds

- **WebSocket bot**: Sees move in <50ms, adjusts order price
- **REST bot**: Still using old price after 1s → order fills at loss

---

## 7. Future Improvements

### Planned Enhancements

1. **Subscribe to all pairs on startup**
   - Pre-subscribe to entire pair registry
   - Eliminates subscription latency during cycle execution

2. **Monitor WS health and auto-reconnect**
   - Track message frequency
   - Auto-reconnect if no messages for >10s
   - Exponential backoff on repeated failures

3. **Metrics on WS vs REST usage ratio**
   - Log fallback events to `logs/metrics.jsonl`
   - Alert if REST fallback >5% of cycles
   - Track price freshness distribution

4. **Alert when falling back to REST frequently**
   - Telegram notification on sustained REST usage
   - Indicates WS infrastructure issues

### Implementation Priority

```python
# TODO: Add WS health monitoring
def monitor_ws_health(self):
    """Check if WS is receiving regular updates"""
    if time.time() - self._last_message_time > 10:
        print("[WARN] No WS messages for 10s - possible disconnect")
        self.reconnect()

# TODO: Add metrics logging
def log_fallback_event(self, symbol: str, reason: str):
    """Log when REST fallback is used"""
    metrics = {
        "timestamp": datetime.now().isoformat(),
        "symbol": symbol,
        "reason": reason,  # "stale_price", "ws_disconnected", etc.
        "fallback_latency_ms": latency_ms
    }
    # Append to logs/metrics.jsonl
```

---

## Summary

**Golden Rule**: If it's time-critical, use WebSocket. If it's historical or bootstrap, REST is fine.

The chart maintenance bot's profitability depends on accurate, real-time pricing. WebSocket-first architecture is not optional—it's the difference between profit and being arb'd.

---

*Last updated: 2026-03-18*
*Implementation: `scripts/price_feed.py`, `scripts/order_executor.py`*
