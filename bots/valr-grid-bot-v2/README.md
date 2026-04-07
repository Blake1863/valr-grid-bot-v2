# VALR Grid Bot v2

Production-grade grid trading bot for VALR perpetual futures. Built from first principles against the documented VALR API.

**Pair-agnostic:** Works with any VALR perpetual futures pair (SOLUSDTPERP, BTCUSDTPERP, ETHUSDTPERP, etc.). Configure the pair in `config.json`.

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

### neutral (Symmetric grid — default)
Places BUY orders below reference price AND SELL orders above reference price.

**BUY orders (below reference):**
```
level_i price = ref * (1 - spacing_pct/100)^i
```

**SELL orders (above reference):**
```
level_i price = ref * (1 + spacing_pct/100)^i
```

**Example:** ref=$80, spacing=0.4%, levels=3:
- BUY 1: $80 × 0.996 = $79.68
- BUY 2: $80 × 0.996² = $79.36
- BUY 3: $80 × 0.996³ = $79.04
- SELL 1: $80 × 1.004 = $80.32
- SELL 2: $80 × 1.004² = $80.64
- SELL 3: $80 × 1.004³ = $80.96

**How neutral mode works:**
1. Grid orders act as natural take-profits for each other
2. When a BUY fills → you're long → existing SELL orders above close the position
3. When a SELL fills → you're short → existing BUY orders below close the position
4. After each fill, bot replenishes the filled side one level deeper
5. Stop-loss placed as conditional order (3% default)
6. **Market-neutral when flat** — no directional bias
7. **Effective leverage** when one side fills: `(levels × qty × price) / (balance × allocation)`

**With 10x leverage config:**
- Total grid notional: balance × 0.9 × 10
- When one side fills (3 orders): ~5x effective exposure
- Example: $35 balance, 0.67 SOL/level, $80 price → 3 × 0.67 × $80 = ~$160 = ~5x

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
| `pair` | string | Any VALR perpetual futures pair (e.g. `"SOLUSDTPERP"`, `"BTCUSDTPERP"`, `"ETHUSDTPERP"`) — **pair-agnostic** |
| `subaccountId` | string | VALR sub-account ID (empty string `""` for main account) |
| `mode` | `neutral` \| `long_only` \| `short_only` | **`neutral`**: symmetric grid (buys + sells), market-neutral when flat. **`long_only`**: buys below price (bullish). **`short_only`**: sells above price (bearish) |
| `levels` | number | Number of grid levels per side (1-20). In `neutral` mode: total orders = `levels × 2` |
| `spacingMode` | `percent` \| `absolute` | Spacing calculation method |
| `spacingValue` | string | Decimal string (e.g. `"0.4"` for 0.4%) |
| `quantityPerLevel` | string | Quantity per grid level (e.g. `"0.67"` SOL). Determines total notional: `qty × levels × 2 × price` |
| `stopLossMode` | `percent` \| `absolute` | SL distance mode |
| `stopLossValue` | string | SL distance (e.g. `"3.0"` for 3%) |
| `tpMode` | `disabled` | TP strategy. In `neutral` mode: grid orders are TPs, so set to `disabled` |
| `triggerType` | `MARK_PRICE` \| `LAST_TRADED` | Trigger for TPSL conditional |
| `referencePriceSource` | `mark_price` \| `mid_price` \| `last_traded` \| `manual` | Grid center price source |
| `leverage` | number | Account leverage (informational only — set account-side on VALR) |
| `postOnly` | boolean | Use postOnly for entries (0% maker fee on VALR futures) |
| `allowMargin` | boolean | Required `true` for futures orders |
| `cooldownAfterStopSecs` | number | Pause after stop-loss trigger (default: 300s) |
| `dryRun` | boolean | Simulate without placing orders |

**Sizing formula for neutral mode:**
```
target_notional = balance × allocation × leverage
qty_per_level = (target_notional / levels) / price
```

Example: $35 balance, 90% allocation, 10x leverage, $80 price, 3 levels:
- Total notional: $35 × 0.9 × 10 = $315
- Qty per level: $315 / 3 / $80 = 0.67 SOL
- Effective leverage when one side fills: ~5x

## Known Limitations and TODOs

1. **Recentering not yet implemented** — bot places grid around startup reference price only. TODO: add recentering logic when price drifts beyond N levels from grid center.
2. **No leverage setting** — VALR leverage is set account-side, not per-order. The config `leverage` field is informational only. Set leverage on VALR account settings.
3. **No margin check** — `MARGIN_INFO` subscription exists in WS but bot doesn't yet stop entries on low margin. TODO: add margin monitoring.
4. **No partial fill tracking** — orders that partially fill are treated as pending until full fill. For grid trading this is acceptable.
5. **Batch orders don't include `allowMargin`** — the batch PLACE_LIMIT data field doesn't document `allowMargin` in the batch schema. If batch orders fail, check if `allowMargin` needs to be set differently in batch context.
6. **202 race condition** — between placing a conditional and receiving WS confirmation, the bot could see "no TPSL" in DB during periodic reconcile. Guarded by `conditionalOrderId` in DB.
7. **Pair-specific testing** — bot is pair-agnostic but primarily tested on SOLUSDTPERP. TODO: test on BTCUSDTPERP, ETHUSDTPERP with different tick sizes.

## Running

```bash
npm install
npm start
```

Or as systemd service — copy the old valr-grid-bot.service and point it at this directory.

```bash
npm test          # Run all 48 tests
npm run build     # TypeScript check only (no emit)
LOG_PRETTY=1 npm start  # Pretty-print logs (requires pino-pretty)
```
