# VALR Grid Bot v3 — OKX/Bybit Style Neutral Futures Grid

A perpetual futures grid bot for VALR that replicates the core mechanics of OKX and Bybit's neutral futures grid bots.

## Key Features

### Grid Range
- **Fixed bounds**: Operates only inside `[lowerBound, upperBound]`
- **Range exit handling**: Stops placing new entry orders when price exits range
- **Range re-entry**: Automatically resumes when price returns to range

### Grid Construction
- **Arithmetic mode**: Equal price difference between adjacent levels
- **Geometric mode**: Equal ratio/percentage between adjacent levels
- **gridCount = intervals**: Matches OKX/Bybit convention (N intervals = N+1 boundaries)

### Neutral Mode
- **Below reference price**: Place BUY orders
- **Above reference price**: Place SELL orders
- **Adjacent-level cycles**:
  - Buy at L[i] fills → place SELL at L[i+1]
  - Sell at L[i] fills → place BUY at L[i-1]

### PnL Tracking
- **Realized profit**: Tracked per completed grid cycle
- **Unrealized PnL**: Separate tracking for open positions
- **Fee-aware**: Cycle profit accounts for trading fees

## Configuration

Edit `configs/bot-config.json`:

```json
{
  "pair": "SOLUSDTPERP",
  "subaccountId": "YOUR_SUBACCOUNT_ID",
  "mode": "neutral",
  "lowerBound": "81.75",
  "upperBound": "90.36",
  "gridCount": 15,
  "gridMode": "arithmetic",
  "referencePrice": "86.00",
  "leverage": 10,
  "capitalAllocationPercent": 90,
  "dynamicSizing": false,
  "quantityPerLevel": "0.15",
  "stopLossMode": "percent",
  "stopLossValue": "3.0",
  "tpMode": "disabled",
  "postOnly": true,
  "dryRun": false
}
```

### Key Parameters

| Parameter | Description |
|-----------|-------------|
| `mode` | `neutral`, `long`, or `short` |
| `lowerBound` / `upperBound` | Trading range boundaries |
| `gridCount` | Number of grid intervals (not levels) |
| `gridMode` | `arithmetic` or `geometric` |
| `referencePrice` | Base price for neutral mode side assignment |
| `quantityPerLevel` | Fixed quantity per grid level |
| `dynamicSizing` | Auto-adjust quantity with balance changes |

## Installation

```bash
cd valr-grid-bot-v3
npm install
```

## Usage

### Environment Variables

```bash
export VALR_API_KEY="your-api-key"
export VALR_API_SECRET="your-api-secret"
```

### Run

```bash
# Development
npm run dev

# Production
npm run build
npm start
```

### Systemd Service

Create `~/.config/systemd/user/valr-grid-bot-v3.service`:

```ini
[Unit]
Description=VALR Grid Bot v3
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/admin/.openclaw/workspace/bots/valr-grid-bot-v3
ExecStart=/usr/bin/node dist/app/main.js
Restart=on-failure
RestartSec=10
Environment=VALR_API_KEY=your-key
Environment=VALR_API_SECRET=your-secret

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable valr-grid-bot-v3
systemctl --user start valr-grid-bot-v3
```

## Architecture

```
src/
├── app/
│   ├── main.ts          # Entry point, orchestration
│   └── logger.ts        # Pino logging
├── config/
│   ├── schema.ts        # Zod validation schema
│   └── loader.ts        # Config loading
├── exchange/
│   ├── restClient.ts    # VALR REST API
│   ├── wsPriceClient.ts # Price WebSocket
│   ├── wsAccountClient.ts # Account WebSocket
│   ├── types.ts         # VALR API types
│   └── pairMetadata.ts  # Tick size, precision
├── strategy/
│   ├── gridBuilder.ts   # Grid construction (arith/geometric)
│   └── gridManager.ts   # OKX/Bybit-style grid logic
└── state/
    └── store.ts         # SQLite persistence
```

## Grid Cycle Example

**Configuration:**
- lowerBound: 100
- upperBound: 400
- gridCount: 3 (intervals)
- referencePrice: 250

**Grid Levels:**
```
Level 0: $100 (boundary)
Level 1: $200 ← BUY (below reference)
Level 2: $300 ← SELL (above reference)
Level 3: $400 (boundary)
```

**Cycle Flow:**
1. Bot places BUY at $200, SELL at $300
2. Price drops, BUY @ $200 fills
3. Bot immediately places SELL @ $300 (completion order)
4. Price rises, SELL @ $300 fills
5. **Cycle complete**: Profit = ($300 - $200) × quantity - fees
6. Bot places new BUY @ $200 for next cycle

## Differences from v2

| Feature | v2 | v3 |
|---------|----|----|
| Grid model | N total orders | N intervals (OKX/Bybit) |
| Grid modes | Linear only | Arithmetic + Geometric |
| Neutral mode | Approximate | Exact OKX/Bybit replica |
| Cycle tracking | Basic | Per-cycle profit accounting |
| Range exit | Continue | Pause new entries |
| State persistence | Minimal | Full SQLite persistence |

## Monitoring

Logs are written to `logs/bot.log` and stdout.

State database: `logs/solusdtperp-state.db`

Query completed cycles:
```sql
SELECT * FROM cycles ORDER BY completedAt DESC LIMIT 10;
SELECT SUM(realizedProfit) FROM cycles;
```

## Safety

- **dryRun**: Test without placing real orders
- **postOnly**: Maker-only orders (no taker fees)
- **staleDataTimeoutMs**: Pause trading if price data stale
- **maxActiveGridOrders**: Limit concurrent orders

## License

MIT
