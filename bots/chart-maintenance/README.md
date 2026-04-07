# Chart Maintenance Bot - Documentation

## Overview

Rust-based chart maintenance bot for VALR that executes wash trades between CM1 and CM2 subaccounts to generate chart volume/price action.

**Key Features:**
- ✅ Cross-account trading (CM1 ↔ CM2) to bypass self-trade prevention
- ✅ Account rotation every 3 cycles (balances inventory between accounts)
- ✅ Balance-aware order sizing (prevents insufficient balance errors)
- ✅ WebSocket pricing (mid + mark average)
- ✅ REST API for order placement
- ✅ Configurable per-pair minimums (fetched from VALR API)
- ✅ 5ms delay between maker/taker orders

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Chart Maintenance Bot                     │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  WebSocket (wss://api.valr.com/ws/trade)                    │
│    → Orderbook streaming (OB_L1_DIFF)                       │
│    → Account updates (fills, balances)                      │
│                                                              │
│  REST API (https://api.valr.com/v1)                         │
│    → POST /orders/limit (place orders)                      │
│    → GET /orders/open (check status)                        │
│    → GET /account/balances (check balances)                 │
│    → GET /public/pairs/spot (fetch pair info)               │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

**Order Flow:**
1. Fetch mid price from WebSocket orderbook
2. Fetch mark price from REST API
3. Calculate price = (mid + mark) / 2
4. Check account balances (80% cap for safety buffer)
5. Place maker order (GTC) on CM1 or CM2
6. Wait 5ms
7. Place taker order (IOC) on opposite account
8. Record cycle, rotate accounts every 3 trades

---

## Configuration

### Config File: `bots/chart-maintenance/config.json`

```json
{
  "test_pair": "SOLUSDTPERP",
  "test_spot_pair": "LINKZAR",
  "phase1_pairs": [
    "BTCUSDTPERP",
    "ETHUSDTPERP",
    "XRPUSDTPERP",
    "DOGEUSDTPERP",
    "SOLUSDTPERP",
    "AVAXUSDTPERP"
  ],
  "phase2_pairs_enabled": [
    "LINKZAR"
  ],
  "phase2_pairs_disabled": [
    "BTCZAR", "ETHZAR", "ETHBTC", ...
  ],
  "cycle_interval_seconds": 15,
  "rebalance_interval_seconds": 3600,
  "qty_range_min_multiplier": 1.1,
  "qty_range_max_multiplier": 3.0,
  "cycles_before_role_switch": 3,
  "inventory_cycles_buffer": 4,
  "rebalance_threshold_cycles": 4,
  "maker_order_delay_ms": 10,
  "usdt_per_account": 500,
  "api_base_url": "https://api.valr.com/v1",
  "ws_trade_url": "wss://api.valr.com/ws/trade"
}
```

### Secrets File: `/home/admin/.openclaw/secrets/cm_secrets.env`

```bash
CM1_API_KEY=your_cm1_api_key
CM1_API_SECRET=your_cm1_api_secret
CM2_API_KEY=your_cm2_api_key
CM2_API_SECRET=your_cm2_api_secret
```

---

## Running the Bot

### Phase 1 (Futures Perps)
```bash
cd /home/admin/.openclaw/workspace/bots/chart-maintenance
./cm_bot --phase1
```

### Phase 2 (Spot Pairs)
```bash
cd /home/admin/.openclaw/workspace/bots/chart-maintenance
./cm_bot --phase2
```

### Test Mode (Single Pair)
```bash
./cm_bot --test          # Uses test_pair (SOLUSDTPERP)
./cm_bot --test-spot     # Uses test_spot_pair (LINKZAR)
```

### Systemd Service (if configured)
```bash
systemctl --user status cm_bot_phase1.service
systemctl --user restart cm_bot_phase1.service
```

---

## Account Setup (CM1/CM2)

### Prerequisites
1. Two VALR subaccounts (CM1 and CM2)
2. Self-trade prevention **waived** between CM1/CM2
3. Both accounts funded with:
   - **ZAR pairs:** ~500-1000 ZAR + some base currency (LINK, BTC, etc.)
   - **USDT pairs:** ~100-200 USDT + some base currency
   - **Futures:** USDT collateral for margin

### Fund Allocation Strategy
- **CM1:** Start with 60% of total funds
- **CM2:** Start with 40% of total funds
- Bot rotates every 3 cycles to balance usage

### Minimum Balance per Pair
| Pair Type | Min ZAR/USDT | Min Base Currency |
|-----------|--------------|-------------------|
| LINKZAR   | 500 ZAR      | 10 LINK           |
| BTCZAR    | 1000 ZAR     | 0.001 BTC         |
| USDT pairs| 200 USDT     | Varies            |

---

## State & Memory

### State File: `bots/chart-maintenance/state.json`

Tracks:
- `maker_sides`: BUY/SELL rotation per pair
- `cycle_counts`: Total cycles executed per pair
- `account_rotation`: Tracks 3-cycle rotation counter
- `maker_account`: Current maker account (CM1/CM2) per pair

**Reset state (if needed):**
```bash
rm /home/admin/.openclaw/workspace/bots/chart-maintenance/state.json
```

### Logs
```
bots/chart-maintenance/logs/cm_bot_rust.log    # Current run logs
bots/chart-maintenance/logs/cancel_orders.log  # Order cancellation log
```

---

## Order Sizing Logic

The bot calculates order quantities as follows:

1. **Random multiplier:** 1.1x to 3.0x of minimum quantity
2. **Balance check:** Cap to 80% of available balance
3. **Fallback:** If requested qty > balance, use minimum qty
4. **Skip:** If even minimum qty can't be afforded

**Example:**
```
LINKZAR min_qty = 0.04 LINK
Random multiplier = 2.5x
Requested = 0.10 LINK

If balance allows: Place 0.10 LINK
If balance = 0.05 LINK: Fall back to 0.04 LINK + warn
If balance = 0.02 LINK: Skip cycle + error
```

---

## Troubleshooting

### Insufficient Balance Errors
**Symptom:** VALR push notifications about insufficient balance

**Causes:**
- Account balance too low for minimum order
- One account depleted faster than the other

**Fixes:**
1. Transfer more funds to CM1/CM2
2. Check balances: `GET /v1/account/balances`
3. Bot should auto-fallback to minimums or skip

### External Fills
**Symptom:** Logs show "EXTERNAL FILL: Taker filled but maker has 0 fills"

**Cause:** Taker IOC hit external liquidity instead of our maker order

**Impact:** Still generates chart volume, but uses external counterparty

**Fixes:**
- Normal occurrence (~10-20% of cycles)
- No action needed unless rate exceeds 50%

### Self-Trade Prevention
**Symptom:** Orders rejected with "self-trade prevention"

**Cause:** CM1/CM2 waiver not active, or using same account for both sides

**Fixes:**
1. Verify CM1/CM2 waiver is active on VALR
2. Ensure bot uses different accounts (check logs for "Maker: CM1 | Taker: CM2")

### WebSocket Disconnections
**Symptom:** "Price feed WS closed" warnings

**Cause:** Network issues or VALR WS restart

**Fixes:**
- Bot auto-reconnects every 5 seconds
- Falls back to REST orderbook if WS unavailable

---

## Performance Metrics

### Expected Fill Rate
- **Internal fills (CM1↔CM2):** 80-90%
- **External fills:** 10-20%

### Latency
- **Maker → Taker delay:** 5ms (configured) + ~10-20ms (REST latency)
- **Total:** ~15-25ms between orders hitting VALR

### Volume Generation
- **Per cycle:** 1-3x minimum order size
- **Per hour (15s interval):** ~240 cycles per pair
- **LINKZAR example:** ~24-72 LINK volume/hour

---

## Development

### Build from Source
```bash
export PATH="$HOME/.rustup/toolchains/stable-x86_64-unknown-linux-gnu/bin:$PATH"
cd /home/admin/.openclaw/workspace/bots/chart-maintenance-rust
cargo build --release
cp target/release/cm_bot ../chart-maintenance/
```

### Key Files
```
bots/chart-maintenance-rust/src/main.rs    # Main bot logic
bots/chart-maintenance/config.json          # Configuration
bots/chart-maintenance/state.json           # Runtime state
```

### Testing
```bash
# Test single pair
./cm_bot --test-spot

# Monitor logs
tail -f logs/cm_bot_rust.log | grep -E "COMPLETE|EXTERNAL|Balance"
```

---

## Quick Reference

### Start Bot
```bash
cd /home/admin/.openclaw/workspace/bots/chart-maintenance
./cm_bot --phase2  # Spot pairs
./cm_bot --phase1  # Futures perps
```

### Check Status
```bash
ps aux | grep cm_bot
tail -50 logs/cm_bot_rust.log
```

### Stop Bot
```bash
pkill -f "cm_bot"
```

### Check Balances
```bash
curl -s "https://api.valr.com/v1/account/balances" \
  -H "X-VALR-API-KEY: $CM1_API_KEY" \
  -H "X-VALR-SIGNATURE: $SIGNATURE" \
  -H "X-VALR-TIMESTAMP: $TIMESTAMP"
```

---

## Contact & Support

- **VALR API Docs:** https://docs.valr.com/
- **VALR API Support:** api@valr.com
- **Bot Issues:** Check logs first, then contact Blake
