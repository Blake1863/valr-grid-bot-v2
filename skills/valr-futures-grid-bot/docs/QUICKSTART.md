# 5-Minute Quick Start

Get the VALR Futures Grid Bot running in under 5 minutes.

## Prerequisites

- **Linux/macOS** (Windows WSL2 works too)
- **Rust** installed ([rustup.rs](https://rustup.rs))
- **VALR account** with futures enabled ([rooibos.dev](https://rooibos.dev))

## Step 1: Unpack

```bash
cd ~
unzip valr-futures-grid-bot.zip
cd valr-futures-grid-bot
```

## Step 2: Run Setup

```bash
chmod +x scripts/setup.sh
./scripts/setup.sh
```

The script will:
- Create encrypted vault for API keys
- Install Python dependencies
- Build the Rust binary
- Prompt for your VALR API credentials

## Step 3: Edit Config (Optional)

```bash
nano config/config.json
```

Default settings:
- **Pair**: SOLUSDTPERP
- **Leverage**: 5x
- **Grid**: 3 levels per side (6 orders total)
- **Spacing**: 0.5% between levels (±1.5% range)
- **Stop-loss**: 3% below entry (exceeds grid range ✅)

### ⚠️ Parameter Validation

**Before running, ensure**:
1. `spacing_pct` is between 0.1–3.0%
2. `max_loss_pct` > (`levels` × `spacing_pct`)

**Invalid example** (SL within grid range):
```json
{
  "levels": 5,
  "spacing_pct": 0.8,
  "max_loss_pct": 3  // ❌ WRONG: 5×0.8=4%, SL should be >4%
}
```

**Valid example**:
```json
{
  "levels": 5,
  "spacing_pct": 0.8,
  "max_loss_pct": 5  // ✅ OK: 5 > 4% grid range
}
```

## Step 4: Run

```bash
./target/release/valr-futures-grid-bot
```

You should see:
```
🤖 VALR Futures Grid Bot v1.0 starting...
Config: SOLUSDTPERP | 5x leverage | 90% balance | 3 levels | 0.5% spacing | max_loss=3%
✅ WS price live: 93.50
Mid: 93.50 | Available: 40.00 USDT | Deploying: 36.00 (90%)
✅ BUY @ 93.27 → abc123...
✅ BUY @ 93.04 → def456...
...
Grid live: 6 orders
```

## Step 5: Monitor

Watch the logs:
```bash
tail -f logs/grid-bot.log
```

Or check VALR dashboard:
- Open orders: https://www.valr.com/orders
- Positions: https://www.valr.com/positions

## What Happens Next

1. **Grid placed**: 6 limit orders (3 buy below, 3 sell above mid-price)
2. **Waiting for fills**: Bot monitors price via WebSocket
3. **On fill**: 
   - Replaces filled order immediately
   - Places stop-loss at 3% below average entry
4. **Every 5 min**: Checks grid, replaces if needed
5. **Every 30 min**: Re-centres grid (refreshes all orders)

## Stopping

Press `Ctrl+C` to stop gracefully. The bot will:
- Keep open orders on the book (they're good until cancelled)
- Keep stop-loss active (protecting your position)

To cancel everything:
```bash
# Via VALR UI
https://www.valr.com/orders → Cancel All

# Or via API (advanced)
curl -X DELETE "https://api.valr.com/v1/orders" ...
```

## Troubleshooting

### "Command not found: cargo"
Install Rust: `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`

### "Module not found: cryptography"
Run: `pip3 install --user cryptography`

### "Invalid API key"
Double-check your credentials:
```bash
python3 scripts/secrets.py get valr_api_key
# Compare with what's in your VALR dashboard
```

### "Insufficient balance"
- Transfer USDT to your VALR spot wallet
- Or reduce `balance_usage_pct` in config

### "429 Too Many Requests"
Wait 1-2 minutes, then restart. Don't rapid-restart the bot.

## Next Steps

- [Read full README](../README.md) for advanced config
- [Adjust grid parameters](../config/config.json) for your risk tolerance
- [Monitor PnL](https://www.valr.com/positions) on VALR

---

**Happy trading! 🚀**

Remember: Grid bots work best in ranging markets. In strong trends, you may accumulate positions against the trend. Always use stop-loss (enabled by default at 3%).
