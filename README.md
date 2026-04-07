# VALR Grid Bot v2

Production-grade grid trading bot for VALR perpetual futures. Built from first principles against the documented VALR API.

## Architecture

```
src/
  config/         Config loading (Zod schema validation)
  exchange/       Auth, REST client, WS account, WS trade, pair metadata, types
  strategy/       Grid builder, position manager, TPSL manager, order manager, reconciliation, risk manager
  state/          SQLite persistence (better-sqlite3)
  app/            Main entry point, logger
tests/            48 unit tests (vitest)
```

## VALR Endpoints Used

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/v1/public/pairs/FUTURE` | Load pair metadata (tick, decimals, margins) |
| GET | `/v1/public/{pair}/marketsummary` | REST price fallback |
| GET | `/v1/account/balances` | Check USDT balance on startup |
| GET | `/v1/positions/open` | Startup reconciliation + periodic refresh |
| GET | `/v1/orders/open` | Startup reconciliation |
| GET | `/v1/orders/conditionals` | Check for stale conditionals |
| POST | `/v1/orders/limit` | Single grid level placement |
| POST | `/v1/orders/market` | Emergency close (reduceOnly) |
| POST | `/v1/batch/orders` | Initial grid placement (up to 20 per batch) |
| POST | `/v1/orders/conditionals` | Place TPSL conditional (OCO) |
| PUT | `/v1/orders/modify` | Order repricing (TODO: implement recentering) |
| DELETE | `/v1/orders/order` | Cancel single order |
| DELETE | `/v1/orders/{pair}` | Cancel all orders for pair |
| DELETE | `/v1/orders/conditionals/conditional` | Cancel specific conditional |
| DELETE | `/v1/orders/conditionals/{pair}` | Cancel all conditionals for pair |

## WebSocket Subscriptions

### Account WS (`wss://api.valr.com/ws/account`)
**Auto-pushed (no subscribe needed):**
- `BALANCE_UPDATE`
- `OPEN_ORDERS_UPDATE`
- `ORDER_PROCESSED`
- `FAILED_ORDER`
- `FAILED_CANCEL_ORDER`
- `OPEN_POSITION_UPDATE` (futures — auto-pushed)
- `POSITION_CLOSED` (futures — auto-pushed)
- `ADD_CONDITIONAL_ORDER`, `REMOVE_CONDITIONAL_ORDER`

**Must subscribe explicitly:**
```json
{"type": "SUBSCRIBE", "subscriptions": [{"event": "ORDER_STATUS_UPDATE"}]}
```

### Trade WS (`wss://api.valr.com/ws/trade`)
```json
{
  "type": "SUBSCRIBE",
  "subscriptions": [
    {"event": "AGGREGATED_ORDERBOOK_UPDATE", "pairs": ["SOLUSDTPERP"]},
    {"event": "MARK_PRICE_UPDATE", "pairs": ["SOLUSDTPERP"]},
    {"event": "MARKET_SUMMARY_UPDATE", "pairs": ["SOLUSDTPERP"]}
  ]
}
```

## Authentication

HMAC-SHA512 over: `timestamp_ms + VERB + path_with_query + body + subaccountId`

```typescript
function signRequest(secret, timestamp, verb, path, body = '', subaccountId = '') {
  const mac = crypto.createHmac('sha512', secret);
  mac.update(timestamp.toString());
  mac.update(verb.toUpperCase());
  mac.update(path);
  mac.update(body);
  mac.update(subaccountId);
  return mac.digest('hex');
}
```

Headers: `X-VALR-API-KEY`, `X-VALR-SIGNATURE`, `X-VALR-TIMESTAMP`, `X-VALR-SUB-ACCOUNT-ID`

WebSocket: same three headers (no sub-account header), sign `timestamp + "GET" + "/ws/account"`.

## Grid Level Calculation

### long_only (BUY orders below reference price)
**Percent spacing:**
```
level_i price = ref * (1 - spacing_pct/100)^i
```
Example: ref=120, spacing=0.4%, levels=3:
- Level 1: 120 * 0.996 = 119.52
- Level 2: 120 * 0.996² = 119.04
- Level 3: 120 * 0.996³ = 118.56

**Absolute spacing:**
```
level_i price = ref - (spacing * i)
```

### short_only (SELL orders above reference price)
Inverse — prices increase from reference.

All prices rounded down to `tickSize`. Quantities truncated to `baseDecimalPlaces`.

## Stop Loss (VALR Futures TPSL)

Uses `POST /v1/orders/conditionals` — the documented conditional order endpoint for futures. Do NOT use stop limit orders (unsupported for futures).

**Long position SL (percent mode):**
```
stopTriggerPrice = averageEntryPrice * (1 - stopLoss%)
```

**Short position SL (percent mode):**
```
stopTriggerPrice = averageEntryPrice * (1 + stopLoss%)
```

Payload:
```json
{
  "pair": "SOLUSDTPERP",
  "quantity": "0",              // "0" = close entire position
  "triggerType": "MARK_PRICE",
  "stopLossTriggerPrice": "116.00",
  "stopLossOrderPrice": "-1",   // "-1" = market execution on trigger
  "takeProfitTriggerPrice": "125.00",
  "takeProfitOrderPrice": "-1"  // OCO when both provided
}
```

**202 Accepted ≠ order live** — always confirm via WS (`ADD_CONDITIONAL_ORDER`) or `GET /v1/orders/conditionals`.

TPSL is rebuilt on: every position update, every fill, startup if position exists.

## Startup Reconciliation

1. Fetch pair metadata — fail if pair inactive
2. Fetch account balances
3. Fetch open positions via REST (exchange state is authoritative)
4. Fetch open orders via REST (VALR ignores `?pair=` filter — filter client-side)
5. Load persisted state from SQLite
6. Reconcile:
   - Exchange has position + local flat → rebuild from exchange
   - Local has orders exchange doesn't → mark stale
   - Exchange has orders with `grid-` prefix → restore from previous run
   - Unknown exchange orders → cancel (orphaned)
7. Connect WS clients
8. Place missing grid orders + TPSL only after reconciliation succeeds

## State Persistence

SQLite database at `state.db` (alongside binary/process cwd).

Tables:
- `grid_orders` — active and historical grid limit orders
- `position_state` — current position snapshot (averageEntryPrice, etc.)
- `tpsl_state` — active conditional TPSL order ID + prices
- `bot_state` — key/value: `cooldown_until`, `last_reconcile`

On restart, state is loaded first. Exchange always wins on conflict.

## Dry Run Mode

Set `"dryRun": true` in config.json.

All order placement calls are logged with `[DRY-RUN]` prefix but NOT sent to exchange. Position state is simulated from WS events. TPSL placement is also simulated.

## What Happens on Restart

1. DB state is loaded
2. Exchange positions are fetched — override any stale local state
3. Exchange open orders are fetched — grid orders from previous run are restored by `grid-` prefix
4. TPSL is checked — if position exists and no TPSL, one is placed
5. Normal operation resumes

If the bot crashed mid-order-placement, some orders may be on exchange but not in DB. These are detected by the `grid-` prefix convention and restored.

## Configuration

| Field | Type | Description |
|-------|------|-------------|
| `pair` | string | e.g. `"SOLUSDTPERP"` |
| `subaccountId` | string | VALR sub-account ID |
| `mode` | `long_only` \| `short_only` | Grid direction |
| `levels` | number | Number of grid levels (1-20) |
| `spacingMode` | `percent` \| `absolute` | Spacing calculation method |
| `spacingValue` | string | Decimal string (e.g. `"0.4"` for 0.4%) |
| `quantityPerLevel` | string | Base quantity per grid level |
| `maxNetPosition` | string | Hard cap on net position |
| `stopLossMode` | `percent` \| `absolute` | SL distance mode |
| `stopLossValue` | string | SL distance value |
| `tpMode` | `one_level` \| `fixed` \| `disabled` | TP strategy (directional modes only; neutral mode uses grid orders) |
| `tpFixedValue` | string | Fixed TP distance (required if `tpMode = 'fixed'`) |
| `triggerType` | `MARK_PRICE` \| `LAST_TRADED` | Trigger for TPSL conditionals |
| `referencePriceSource` | `mark_price` \| `mid_price` \| `last_traded` \| `manual` | Grid center |
| `postOnly` | boolean | Use postOnly for entries (0% maker fee) |
| `allowMargin` | boolean | Required `true` for futures subaccount |
| `cooldownAfterStopSecs` | number | Pause after stop-loss trigger |
| `dryRun` | boolean | Simulate without placing orders |

## Closing Order Strategy

### Long Only & Short Only Modes
- **Always have TP orders** — even if `tpMode` is set to `disabled`, bot defaults to grid spacing as the TP distance
- TP is placed as a conditional market order that closes the entire position
- SL is placed as a complementary conditional (OCO with TP)
- Both TP and SL use the VALR conditional order type (POST `/v1/orders/conditionals`)

### Neutral Mode
- **Grid orders ARE the closing mechanism** — symmetric BUY/SELL orders naturally close positions as price oscillates
- When a BUY fills → net long → existing SELL orders above act as TPs
- When a SELL fills → net short → existing BUY orders below act as TPs
- After each fill: replenish the filled side with a new order one level deeper
- **Only SL conditional is placed** for emergency protection (no separate TP conditional needed)
- No additional orders are placed after fills (except replenishment of entry side)

## Known Limitations and TODOs

1. **Recentering not yet implemented** — bot places grid around startup reference price only. TODO: add recentering logic when price drifts beyond N levels from grid center.
2. **No leverage setting** — VALR leverage is set account-side, not per-order. The config `leverage` field is informational only.
3. **No margin check** — `MARGIN_INFO` subscription exists in WS but bot doesn't yet stop entries on low margin.
4. **No partial fill tracking** — orders that partially fill are treated as pending until full fill. For grid trading this is acceptable.
5. **Batch orders don't include `allowMargin`** — the batch PLACE_LIMIT data field doesn't document `allowMargin` in the batch schema. If batch orders fail, check if `allowMargin` needs to be set differently in batch context.
6. **202 race condition** — between placing a conditional and receiving WS confirmation, the bot could see "no TPSL" in DB during periodic reconcile. Guarded by `conditionalOrderId` in DB.

## Getting Started

### Prerequisites

- **Node.js** 18+ (with npm)
- **VALR API credentials** (API key + secret)
  - Get these from [VALR Settings → API Keys](https://app.valr.com/settings/api)
  - Keep your API secret confidential

### Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/Blake1863/valr-grid-bot-v2.git
   cd valr-grid-bot-v2
   ```

2. **Install dependencies:**
   ```bash
   npm install
   ```

3. **Set up environment variables:**
   ```bash
   cp .env.example .env
   ```

4. **Edit `.env` with your VALR API credentials:**
   ```bash
   nano .env
   ```
   
   Add your VALR API key and secret:
   ```
   VALR_API_KEY=your_actual_api_key
   VALR_API_SECRET=your_actual_api_secret
   ```

5. **Update `config.json` with your trading parameters:**
   - `pair`: Trading pair (e.g., `SOLUSDTPERP`)
   - `levels`: Number of grid levels
   - `spacingValue`: Grid spacing (%)
   - `quantityPerLevel`: Size per grid level
   - See [Configuration](#configuration) section for all options

6. **Start the bot:**
   ```bash
   npm start
   ```

### Running Commands

```bash
npm install       # Install dependencies
npm start         # Start the bot
npm test          # Run all 48 unit tests
npm run build     # TypeScript check (no emit)
LOG_PRETTY=1 npm start  # Pretty-print logs (requires pino-pretty)
```

### Running as a Systemd Service

Create `/etc/systemd/system/valr-grid-bot-v2.service`:

```ini
[Unit]
Description=VALR Grid Bot v2
After=network.target

[Service]
Type=simple
User=grid-bot
WorkingDirectory=/opt/valr-grid-bot-v2
ExecStart=/usr/bin/npm start
Restart=on-failure
RestartSec=10

# Load environment variables from .env file
EnvironmentFile=/opt/valr-grid-bot-v2/.env

# Security best practices
ProtectSystem=strict
ProtectHome=yes
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

Then enable and start:
```bash
sudo systemctl enable valr-grid-bot-v2
sudo systemctl start valr-grid-bot-v2
sudo systemctl status valr-grid-bot-v2
```

## Security Best Practices

⚠️ **IMPORTANT**: 
- **Never commit `.env` file** — it contains your API credentials
- **Keep API secret safe** — treat it like a password
- **Use read-only subaccount** if possible — limits damage if API keys are compromised
- **Rotate API keys regularly** — disable old keys in VALR settings
- **Monitor orders** — check VALR dashboard regularly for unexpected activity

## Troubleshooting

**"Environment variable VALR_API_KEY is not set"**
- Make sure `.env` file exists and is properly configured
- Verify you've set `VALR_API_KEY` and `VALR_API_SECRET`

**"Pair not found"**
- Check that the pair in `config.json` is valid on VALR
- Ensure it's a perpetual pair (e.g., `SOLUSDTPERP`)

**Orders not placing**
- Check VALR account balance (need sufficient USDT)
- Verify subaccount is set to allow margin trading
- Check bot logs for detailed error messages
