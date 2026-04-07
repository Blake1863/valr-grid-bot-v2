---
name: valr-futures-grid-bot
description: VALR perpetual futures grid bot in Rust. Auto-manages stop-losses, reconciles book exposure, and self-heals via systemd. Use for market-making on VALR futures with configurable leverage, levels, and spacing.
---

# VALR Futures Grid Bot

Rust-based grid trading bot for VALR perpetual futures. Places symmetric limit orders around mid-price, manages native stop-losses, and auto-reconciles book exposure.

## Key Features

- **Native stop-loss**: Uses VALR conditional orders (`/v1/orders/conditionals`) for SL
- **Book reconciliation**: Health check verifies `position + bids - asks ≈ 0`
- **Auto-healing**: systemd service with `Restart=always`
- **Hot reload**: Config reloaded every 5 min without restart
- **POST-only**: All limit orders are maker (0% fee)
- **Symmetric sizing**: Uniform quantity per level (no drift)

## Architecture

```
bots/valr-grid-bot/
├── src/main.rs          # Main bot logic
├── Cargo.toml           # Rust dependencies
├── config.json          # Grid parameters (hot-reloaded)
├── logs/                # Runtime logs
└── target/release/      # Compiled binary
```

## Configuration

Edit `bots/valr-grid-bot/config.json`:

```json
{
  "pair": "SOLUSDTPERP",
  "leverage": 5,
  "balance_usage_pct": 90.0,
  "levels": 3,
  "spacing_pct": 0.4,
  "max_loss_pct": 3.0
}
```

| Parameter | Description | Default |
|-----------|-------------|---------|
| `pair` | VALR futures pair | SOLUSDTPERP |
| `leverage` | Leverage multiplier | 5 |
| `balance_usage_pct` | % of USDT to deploy | 90 |
| `levels` | Grid levels per side | 3 |
| `spacing_pct` | % between levels | 0.4 |
| `max_loss_pct` | Stop-loss % from entry | 3.0 |

### Recommended Profiles (SOLUSDTPERP, 14-day backtest)

| Profile | Levels | Spacing | PnL | Sharpe | Fills | Use Case |
|---------|--------|---------|-----|--------|-------|----------|
| Conservative | 5 | 1.0% | +$1,474 | 3.03 | 22 | Safety first, low activity |
| **Moderate** | **3** | **0.4%** | **+$7,281** | **1.18** | **166** | **Balanced (default)** |
| Aggressive | 3 | 0.3% | +$7,366 | 0.99 | 251 | Max PnL, high activity |

**Default:** Moderate (3 levels, 0.4% spacing) — highest PnL with Sharpe >1.0

## Running the Bot

### Via systemd (recommended)

```bash
# Check status
systemctl --user status valr-grid-bot.service

# View live logs
journalctl --user -u valr-grid-bot.service -f

# Restart
systemctl --user restart valr-grid-bot.service

# Stop
systemctl --user stop valr-grid-bot.service
```

### Manual (dev only)

```bash
cd /home/admin/.openclaw/workspace/bots/valr-grid-bot
./target/release/valr-grid-bot
```

## Building

```bash
cd /home/admin/.openclaw/workspace/bots/valr-grid-bot
cargo build --release
# Binary: ../target/release/valr-grid-bot
```

**Important**: Workspace builds to `bots/target/release/`, not `valr-grid-bot/target/release/`.

## Health Check Logic

Every 5 minutes, the bot verifies:

1. **Book balance**: `position_qty + bid_qty - ask_qty ≈ 0`
2. **Stop-loss present**: If position open, SL order exists
3. **Take-profit present**: If unhedged, places conditional TP at 0.5% profit

If unhedged:
- Cancels all orders
- Places emergency stop-loss (conditional order)
- Places take-profit (conditional order, 0.5% from entry)
- Both SL and TP auto-cancel when position closes
- Logs warning with exposure breakdown

**Why conditional TP?** A resting limit order could fill after stop-out if price reverses. Conditional TP ensures both exit orders are atomic — when one triggers, the other cancels automatically.

## VALR API Endpoints Used

| Endpoint | Purpose | Rate Limit |
|----------|---------|------------|
| `GET /v1/positions/open` | Check position | 2,000/min |
| `GET /v1/orders/open` | List orders | 2,000/min |
| `POST /v1/orders` | Place limit order | 400/s |
| `DELETE /v1/orders/order` | Cancel order | 450/s |
| `POST /v1/orders/conditionals` | Place SL | 400/s |
| `DELETE /v1/orders/conditionals` | Cancel SL | 450/s |
| `GET /v1/public/{pair}/buckets` | OHLCV (fallback) | 30/min |

## WebSockets

- **Trade WS**: `wss://api.valr.com/ws/trade` — `OB_L1_DIFF` for real-time price
- **Account WS**: `wss://api.valr.com/ws/account` — `ORDER_STATUS_UPDATE` for fills

## Troubleshooting

### Bot not starting

```bash
# Check systemd status
systemctl --user status valr-grid-bot.service

# Check logs
journalctl --user -u valr-grid-bot.service -n 50

# Verify binary exists
ls -la bots/valr-grid-bot/target/release/valr-grid-bot
```

### Stale logs

If logs haven't updated in >1 min, process likely crashed:

```bash
# Restart
systemctl --user restart valr-grid-bot.service

# Watch for errors
journalctl --user -u valr-grid-bot.service -f
```

### Stop-loss not placed

Check health check logs for:
- `BOOK IS UNHEDGED` warning
- `Stop-loss placed @ X` confirmation
- `Stop-loss verified → ID` success

If missing, bot may have crashed before placement — restart and verify.

## Lessons Learned (2026-03-17)

1. **Sync position on startup**: Fill may happen before bot starts tracking — fetch from REST API
2. **Verify builds**: Always check binary timestamps after build/copy (`stat path | grep Modify`)
3. **Use systemd**: Manual processes die silently; systemd auto-restarts on crash
4. **Book reconciliation**: Health check must verify net exposure, not just cached state
5. **Closing orders**: Only place when unhedged (remedy), not on every check

## References

- VALR API docs: https://api-docs.rooibos.dev/
- Workspace: `/home/admin/.openclaw/workspace/bots/valr-grid-bot/`
- Service: `~/.config/systemd/user/valr-grid-bot.service`
