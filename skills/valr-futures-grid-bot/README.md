# VALR Futures Grid Bot

Automated grid trading bot for VALR perpetual futures with native stop-loss protection.

## Features

- **Symmetric Grid Strategy**: Places balanced buy/sell limit orders around mid-price
- **Native Stop-Loss**: Uses VALR conditional orders (TP/SL) — monitored by VALR, not your bot
- **Trailing Stop**: Automatically adjusts stop-loss as position average entry changes
- **Hot-Reload Config**: Change grid parameters without restarting
- **Post-Only Orders**: Maker fees = 0% (you get rebates)
- **Position-Aware**: Detects existing positions/orders on startup, doesn't double-up

## Quick Start

### Prerequisites

1. **Rust** (1.70+): `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`
2. **VALR API Keys**: Create at https://rooibos.dev with futures trading permissions
3. **Python 3** (for secrets management)

### Setup

#### 1. Store API Credentials

The bot uses VALR's secrets vault. Create `/path/to/secrets.py`:

```python
#!/usr/bin/env python3
import sys, os, json
from cryptography.fernet import Fernet

KEYFILE = os.path.expanduser('~/.valr-bot/.key')
VAULT   = os.path.expanduser('~/.valr-bot/vault.json')

def load_fernet():
    with open(KEYFILE, 'rb') as f:
        return Fernet(f.read())

def get_secret(name):
    with open(VAULT) as f:
        vault = json.load(f)
    f = load_fernet()
    return f.decrypt(vault[name].encode()).decode()

if __name__ == '__main__':
    # Initialize: python3 secrets.py init <api_key> <api_secret>
    if len(sys.argv) == 4 and sys.argv[1] == 'init':
        f = load_fernet()
        vault = {'valr_api_key': f.encrypt(sys.argv[2].encode()).decode(),
                 'valr_api_secret': f.encrypt(sys.argv[3].encode()).decode()}
        with open(VAULT, 'w') as f: json.dump(vault, open(VAULT, 'w'), indent=2)
        os.chmod(VAULT, 0o600)
        print('✅ Credentials stored')
    elif len(sys.argv) == 3 and sys.argv[1] == 'get':
        print(get_secret(sys.argv[2]))
    else:
        print('Usage: python3 secrets.py init <key> <secret> | get <name>')
```

Initialize:
```bash
mkdir -p ~/.valr-bot
openssl rand -base64 32 > ~/.valr-bot/.key
chmod 600 ~/.valr-bot/.key
python3 secrets.py init YOUR_API_KEY YOUR_API_SECRET
```

#### 2. Configure Grid Parameters

Edit `config/config.json`:

```json
{
  "pair": "SOLUSDTPERP",
  "leverage": 5,
  "balance_usage_pct": 90,
  "levels": 3,
  "spacing_pct": 0.5,
  "max_loss_pct": 3
}
```

| Parameter | Description | Example |
|---|---|---|
| `pair` | Trading pair | `SOLUSDTPERP`, `BTCUSDTPERP` |
| `leverage` | Leverage multiplier | `5` (5x) |
| `balance_usage_pct` | % of USDT to deploy | `90` (keep 10% buffer) |
| `levels` | Orders per side | `3` (6 total orders) |
| `spacing_pct` | Gap between levels | `0.5` (0.5%) |
| `max_loss_pct` | Stop-loss distance | `3` (3% below entry) |

#### 3. Build

```bash
cd valr-futures-grid-bot
cargo build --release
```

Binary: `target/release/valr-futures-grid-bot`

#### 3b. Validate Config (Recommended)

```bash
python3 scripts/validate_config.py config/config.json
```

Checks:
- ✅ Spacing within 0.1–3.0%
- ✅ Stop-loss exceeds grid range
- ✅ Reasonable leverage and capital usage
- ✅ Risk score assessment

#### 4. Run

```bash
cd valr-futures-grid-bot
./target/release/valr-futures-grid-bot
```

Logs: `logs/grid-bot.log`

## How It Works

### Grid Placement

```
Mid-Price: $93.50

SELL 3  $93.97  (+0.5%)
SELL 2  $93.74  (+0.25%)
SELL 1  $93.50  (mid)
─────────────────
BUY  1  $93.27  (-0.25%)
BUY  2  $93.04  (-0.5%)
BUY  3  $92.81  (-0.75%)
```

### Order Flow

1. **Grid placed**: 6 limit orders (3 buy, 3 sell)
2. **Fill detected**: e.g., BUY 1 @ $93.27
3. **Replacement**: Immediately re-place BUY 1 @ $93.27
4. **Stop-loss**: Place SELL conditional @ 3% below new avg entry
5. **Repeat**: Each fill triggers replacement + SL update

### Stop-Loss Logic

- **Long position**: SL trigger = avg_entry × (1 - max_loss_pct/100)
- **Short position**: SL trigger = avg_entry × (1 + max_loss_pct/100)
- **Quantity**: `0` (closes entire position — no size tracking needed)
- **Execution**: Market order (`-1`) when triggered

## Configuration Examples

### Parameter Constraints

| Parameter | Valid Range | Notes |
|---|---|---|
| `spacing_pct` | 0.1 – 3.0% | <0.1% = too tight, >3% = too wide |
| `max_loss_pct` | > (levels × spacing) | Must exceed grid range to avoid premature SL |
| `levels` | 2 – 15 | More levels = more fills, less capital per order |
| `leverage` | 1 – 20x | Higher = more profit AND more liquidation risk |

**Grid Range Formula**: `grid_range = levels × spacing_pct`

**Example**: 5 levels × 0.5% spacing = 2.5% total range (±2.5% from mid)
- Stop-loss must be >2.5% (e.g., 3–5%)

### Conservative (Capital Preservation)
```json
{
  "pair": "BTCUSDTPERP",
  "leverage": 3,
  "balance_usage_pct": 60,
  "levels": 10,
  "spacing_pct": 0.25,
  "max_loss_pct": 3
}
```
- **Grid range**: 2.5% (±2.5% from mid)
- **Expected**: Sharpe >2.0, DD <15%, steady returns
- **Best for**: Low volatility, ranging markets

### Balanced (Risk-Adjusted Growth)
```json
{
  "pair": "SOLUSDTPERP",
  "leverage": 5,
  "balance_usage_pct": 90,
  "levels": 5,
  "spacing_pct": 0.5,
  "max_loss_pct": 4
}
```
- **Grid range**: 2.5% (±2.5% from mid)
- **Expected**: Sharpe 1.5–2.0, DD 15–25%
- **Best for**: Most market conditions

### Aggressive (Max Profit)
```json
{
  "pair": "SOLUSDTPERP",
  "leverage": 10,
  "balance_usage_pct": 95,
  "levels": 3,
  "spacing_pct": 1.0,
  "max_loss_pct": 6
}
```
- **Grid range**: 3.0% (±3% from mid)
- **Expected**: Sharpe 1.0–1.5, DD 25–40%, high profit
- **Best for**: High volatility, strong conviction

### Scalping (High Frequency)
```json
{
  "pair": "ETHUSDTPERP",
  "leverage": 5,
  "balance_usage_pct": 90,
  "levels": 10,
  "spacing_pct": 0.15,
  "max_loss_pct": 3
}
```
- **Grid range**: 1.5% (±1.5% from mid)
- **Expected**: Many small profits, requires low fees
- **Best for**: Tight ranges, low volatility

## Manual Management

### Check Active Orders
```bash
curl -s "https://api.valr.com/v1/orders/open?currencyPair=SOLUSDTPERP" \
  -H "X-VALR-API-KEY: YOUR_KEY" \
  -H "X-VALR-SIGNATURE: SIGNATURE" \
  -H "X-VALR-TIMESTAMP: TIMESTAMP"
```

### Check Conditional Orders (TP/SL)
```bash
curl -s "https://api.valr.com/v1/orders/conditionals?currencyPair=SOLUSDTPERP" \
  -H "X-VALR-API-KEY: YOUR_KEY" \
  -H "X-VALR-SIGNATURE: SIGNATURE" \
  -H "X-VALR-TIMESTAMP: TIMESTAMP"
```

### Cancel All Orders
```bash
# Use the Python helper script
python3 scripts/manage_orders.py --action cancel-all --pair SOLUSDTPERP
```

## Monitoring

### Key Log Messages

| Message | Meaning |
|---|---|
| `✅ Grid live: 6 orders` | Bot is running normally |
| `📥 Fill: 0.21 SOL @ 93.27` | Order filled, position opened/adjusted |
| `🛡️ Stop-loss verified` | SL placed and confirmed on VALR |
| `⟳ Re-centring grid` | Config changed or timer fired, refreshing orders |
| `⚠️ Low USDT balance` | Need more margin to continue |

### Health Checks

Bot performs these automatically:
- Every 5 min: Check for filled orders, replace if needed
- Every 30 min: Re-centre grid (cancel + re-place with updated config)
- On startup: Detect existing positions, place SL if missing

## Troubleshooting

### "Stop loss trigger price invalid"
- SOL needs 2 decimal places, BTC needs 0
- Bot handles this automatically — check your config doesn't override

### "Side not supported on FUTURE pairs"
- Don't include `side` parameter for futures conditional orders
- VALR infers from existing position

### "Insufficient balance"
- Increase USDT in subaccount
- Reduce `balance_usage_pct` or `leverage` in config

### "429 Too Many Requests"
- Bot has built-in rate limit handling (retries with backoff)
- Don't restart rapidly — wait 1-2 minutes between restarts

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                 VALR Grid Bot                        │
├─────────────────────────────────────────────────────┤
│  Trade WS (OB_L1_DIFF)  →  Real-time mid-price     │
│  Account WS (ORDER_STATUS) →  Fill events (~8ms)   │
│  REST API               →  Order placement         │
├─────────────────────────────────────────────────────┤
│  Grid Manager: Places symmetric limit orders       │
│  Fill Handler: Replaces filled orders + updates SL │
│  SL Manager: Trails stop at fixed % from entry     │
└─────────────────────────────────────────────────────┘
         ↓
┌─────────────────────────────────────────────────────┐
│              VALR Conditional Orders                │
│  - Monitored by VALR (not bot)                     │
│  - Triggers on mark price                          │
│  - Executes as market order                        │
│  - quantity=0 closes entire position               │
└─────────────────────────────────────────────────────┘
```

## Security

- **API keys**: Encrypted at rest (AES-256 via Fernet)
- **No credentials in config**: All sensitive data in vault
- **Read-only logs**: No keys or secrets written to logs
- **Subaccount recommended**: Isolate bot trading from main account

## API Rate Limits

| Endpoint | Limit | Bot Usage |
|---|---|---|
| POST /v1/orders | 400/s | Grid placement (~6 per cycle) |
| DELETE /v1/orders | 450/s | Order cancellation |
| POST /v1/orders/conditionals | 400/s | SL placement (per fill) |
| GET /v1/orders/open | 2,000/min | Health checks (every 5 min) |

## Support

- VALR API Docs: https://api-docs.rooibos.dev/
- VALR Developer Portal: https://rooibos.dev/
- Issues: Report on GitHub

## License

MIT License — use at your own risk. Crypto trading involves substantial risk of loss.

---

**Version**: 1.0.0  
**Last Updated**: 2026-03-17
