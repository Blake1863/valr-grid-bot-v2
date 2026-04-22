# Build Spec — VALR Perpetual Grid Bot (v4)

You are building **a complete, production-ready perpetual futures grid trading bot** for the VALR exchange in TypeScript/Node.js. This is a rewrite of a broken v3 — do not read v3, do not import from it. Start fresh.

## Where to build

- **Repo path (local):** `/home/admin/.openclaw/workspace/bots/valr-perpetual-grid-bot/`
- **Create a new git repo** inside that directory (`git init`)
- **Create a new public GitHub repo** named `valr-perpetual-grid-bot` under the user's existing GitHub account (they push via SSH; `git@github.com:Blake1863/valr-perpetual-grid-bot.git`)
- **License:** MIT (add `LICENSE` file, copyright "Blake" 2026)
- **Readme:** comprehensive (see §10)

You do NOT need to create the GitHub repo — the user will do that and push. Just commit locally with a clean history and write `README.md` with the remote instructions.

## Target runtime

- Node.js 22+
- TypeScript (strict mode), ESM modules, `tsconfig.json` similar to v3
- Dependencies: `pino`, `decimal.js`, `ws`, `better-sqlite3`, `zod` (for config validation), `dotenv`
- Build to `dist/` with `tsc`; run with `node dist/app/main.js`
- Use systemd user services (template provided in §12)

## The spec (authoritative)

```
CORE OBJECTIVE
Maintain a continuous grid of limit orders within a defined price range on
perpetual futures, capture spread through repeated buy-low/sell-high execution.
The system must always keep N active orders in the market.

GRID SETUP
- Lower bound L, Upper bound U, Number of grid orders N
- Spacing: geometric (default) or arithmetic
- Divide [L, U] into N+1 intervals (N+2 boundary points)
- Inner N levels are orderable; boundary levels 0 and N+1 are range limits

ORDER PLACEMENT
- At all times:
  - BUY orders at levels where price < currentPrice
  - SELL orders at levels where price > currentPrice
- Total active orders must ALWAYS = N (when capital allows)

DYNAMIC INVENTORY BIAS
- The grid must NOT remain symmetric
- Price moves up → more BUY levels below, fewer SELL levels above
- Price moves down → more SELL levels above, fewer BUY levels below
- Always maintain full coverage in [L, U]
- Sides are recomputed every tick from (level_price, current_price)

EXECUTION LOOP (on fill events)
- When BUY at level i fills → place SELL at level i+1
- When SELL at level i fills → place BUY at level i-1
- (Implementation: the next reconciliation tick handles this via plan-vs-reality
  diff — do NOT replace inline to avoid races)

CAPITAL MANAGEMENT
- Allocate capital evenly across grid levels
- Fixed order size per level (auto-computed via dynamic sizing formula in §5)
- Use leverage as configured (must match exchange-side leverage tier)
- Margin safety check before every batch placement

RISK MANAGEMENT
- Range exit: HALT (cancel all orders, pause new placements, wait for re-entry)
- Stop loss: configurable percentage from AVERAGE POSITION ENTRY PRICE
  Default 3%. When triggered: cancel all orders, market-close position, halt.
- Monitor: margin ratio, liquidation proximity, funding rate awareness

CONSTRAINTS
- Never leave gaps in the grid
- Never exceed N active orders
- Replace filled orders (on next tick, via diff)
- Post-fill placement respects grid structure

GOAL
Exploit mean-reverting price action within a bounded range by continuously
harvesting spread while maintaining controlled inventory exposure.
```

## Module structure

```
valr-perpetual-grid-bot/
├── src/
│   ├── app/
│   │   ├── main.ts              # Orchestrator, lifecycle, graceful shutdown
│   │   ├── supervisor.ts        # Stop-loss, range-exit, margin/liq monitoring, alerts
│   │   └── logger.ts            # Pino JSON + dedupe wrapper for repeated errors
│   ├── config/
│   │   ├── schema.ts            # Zod schema (see §3)
│   │   └── loader.ts            # Load + validate + substitute env vars
│   ├── exchange/
│   │   ├── restClient.ts        # VALR REST: orders, balances, positions, leverage
│   │   ├── wsPriceClient.ts     # Mark price / ticker WebSocket
│   │   ├── wsAccountClient.ts   # Account event stream (fills, order updates)
│   │   ├── pairMetadata.ts      # Tick size, qty precision, min qty per pair
│   │   └── types.ts             # Shared TS types
│   ├── strategy/
│   │   ├── grid.ts              # PURE: buildLevels(L, U, N, mode) → Level[]
│   │   ├── plan.ts              # PURE: planDesiredOrders(levels, price, qty) → DesiredOrder[]
│   │   ├── reconciler.ts        # Diff(desired, exchangeOpenOrders) → {place, cancel}
│   │   └── cycles.ts            # Compute realised profit when entry+exit pair
│   ├── state/
│   │   ├── store.ts             # SQLite: orders, cycles, metrics tables
│   │   └── migrations.ts        # Schema version 1, create tables if missing
│   └── alerts/
│       └── telegram.ts          # Thin HTTP helper → sends to OpenClaw gateway channel
├── configs/
│   ├── sol.example.json         # Example SOL config (no secrets)
│   └── eth.example.json         # Example ETH config (no secrets)
├── scripts/
│   ├── reset-state.ts           # Cancel all + wipe SQLite (CLI tool)
│   ├── status.ts                # Print snapshot: grid, position, PnL, open orders
│   └── dry-run.ts               # Builds grid, prints what WOULD be placed, no API
├── systemd/
│   ├── valr-perpetual-grid-bot@.service   # Templated unit file (instance = config name)
│   └── install.sh               # Copies to ~/.config/systemd/user/
├── .env.example                 # API keys placeholders
├── .gitignore                   # node_modules, dist, logs, *.db, .env, configs/*.json (except .example)
├── package.json
├── tsconfig.json
├── LICENSE                      # MIT, copyright Blake 2026
└── README.md                    # Full usage docs (see §10)
```

## 1. Configuration schema (`src/config/schema.ts`)

Use Zod. All numeric fields as strings internally to preserve precision; convert to Decimal at use site.

```typescript
export const BotConfigSchema = z.object({
  // === Required user inputs ===
  pair: z.string().regex(/[A-Z]+USDT?PERP$/),       // e.g. "SOLUSDTPERP"
  subaccountId: z.string(),
  gridCount: z.number().int().min(2).max(200),       // N — number of ORDERS
  lowerBound: z.string(),                            // e.g. "82.00"
  upperBound: z.string(),                            // e.g. "92.00"
  stopLossPercent: z.number().min(0).max(50).default(3.0),

  // === Grid ===
  gridMode: z.enum(['geometric', 'arithmetic']).default('geometric'),
  referencePrice: z.string().optional(),             // Optional: defaults to current mark

  // === Capital ===
  leverage: z.number().min(1).max(60).default(10),
  capitalAllocationPercent: z.number().min(1).max(100).default(100),
  reservePercent: z.number().min(0).max(50).default(10),
  dynamicSizing: z.boolean().default(true),
  quantityPerLevel: z.string().optional(),           // Required if dynamicSizing=false

  // === Risk ===
  onRangeExit: z.enum(['halt', 'close_and_reset']).default('halt'),
  stopLossReference: z.enum(['avg_entry', 'disabled']).default('avg_entry'),
  marginRatioAlertPercent: z.number().default(80),
  liquidationProximityPercent: z.number().default(10),
  consecutiveFailuresThreshold: z.number().default(20),
  consecutiveFailuresWindowSecs: z.number().default(60),
  cooldownAfterStopSecs: z.number().default(300),

  // === Execution ===
  postOnly: z.boolean().default(true),
  allowMargin: z.boolean().default(false),
  triggerType: z.enum(['MARK_PRICE', 'LAST_PRICE']).default('MARK_PRICE'),
  referencePriceSource: z.enum(['mark_price', 'last_price']).default('mark_price'),

  // === Tuning ===
  reconcileIntervalSecs: z.number().default(10),
  staleDataTimeoutMs: z.number().default(30000),
  maxPlacementsPerSec: z.number().default(5),
  dryRun: z.boolean().default(false),

  // === Alerts ===
  alertChannel: z.enum(['telegram', 'log', 'both', 'none']).default('both'),
  telegramGatewayUrl: z.string().optional(),         // OpenClaw gateway endpoint
  telegramChatId: z.string().optional(),
});
export type BotConfig = z.infer<typeof BotConfigSchema>;
```

**Only 4 fields are truly required from the user:**
- `pair`
- `subaccountId`  
- `gridCount` (N)
- `lowerBound` / `upperBound` (range)

`stopLossPercent` defaults to 3.0 (user can override).
Everything else has sensible defaults.

## 2. Grid construction (`src/strategy/grid.ts`)

Pure function, no side effects, fully deterministic.

```typescript
export interface Level {
  index: number;              // 0..N+1 (0 and N+1 are boundaries, unorderable)
  price: Decimal;
  priceStr: string;           // tick-rounded, exchange-formatted
}

export function buildLevels(
  lowerBound: Decimal,
  upperBound: Decimal,
  gridCount: number,          // = N, the number of ORDERS desired
  mode: 'geometric' | 'arithmetic',
  constraints: PairConstraints
): Level[]
```

- Generate `gridCount + 2` points: boundaries at indices `0` and `gridCount+1`, inner levels at `1..gridCount`.
- Geometric: `ratio = (U/L)^(1/(N+1))`, `level[i] = L * ratio^i`
- Arithmetic: `step = (U-L)/(N+1)`, `level[i] = L + step*i`
- Round each price to the pair's `tickSize`.
- Deduplicate on `priceStr`; if dedup reduces count, re-index and warn (but don't fail).
- Always returns levels sorted ascending by price.

## 3. Planning (`src/strategy/plan.ts`)

Pure function. Given current state, return the full desired set of orders.

```typescript
export interface DesiredOrder {
  levelIndex: number;          // 1..N (boundaries excluded)
  side: 'BUY' | 'SELL';
  price: Decimal;
  priceStr: string;
  quantity: Decimal;
  quantityStr: string;
  customerOrderId: string;     // deterministic from run_id + level + side
}

export function planDesiredOrders(
  levels: Level[],             // from buildLevels
  currentPrice: Decimal,
  quantityPerLevel: Decimal,
  runId: string,
  constraints: PairConstraints
): DesiredOrder[]
```

Logic:
- Skip levels 0 and last (boundaries).
- For each inner level i:
  - If `level.price < currentPrice` → BUY
  - If `level.price > currentPrice` → SELL
  - If equal → skip (tick-rounding tiebreaker)
- Generate `customerOrderId = gridv4-{side[0]}{level}-{runId}` (max 50 chars).
- Return full set.

This implements the **dynamic inventory bias**: recomputed every tick from (level, price).

## 4. Reconciliation (`src/strategy/reconciler.ts`)

Pure function. Diff desired vs exchange reality.

```typescript
export interface ReconcilePlan {
  toPlace: DesiredOrder[];       // desired but not on exchange
  toCancel: ExchangeOrder[];     // on exchange but not in desired (or wrong side/price)
  unchanged: number;             // count of matches — for metrics
}

export function reconcile(
  desired: DesiredOrder[],
  exchangeOpenOrders: ExchangeOrder[],   // fetched from VALR, filtered to this pair
  runId: string                          // to ignore orphaned orders from prior runs
): ReconcilePlan
```

Match criterion: `customerOrderId` prefix matches `gridv4-{side}{level}-{runId}`.
- If an exchange order exists with a different side/price than desired for that level → cancel + place fresh (treat as mismatch).
- If an exchange order exists from a different `runId` → cancel it (stale).

## 5. Dynamic sizing

```typescript
export function computeQuantityPerLevel(
  freeMargin: Decimal,
  config: BotConfig,
  referencePrice: Decimal,
  constraints: PairConstraints
): Decimal {
  const usable = freeMargin
    .mul(config.capitalAllocationPercent).div(100)
    .mul(new Decimal(100).minus(config.reservePercent)).div(100);
  
  const totalNotional = usable.mul(config.leverage);
  const perOrderNotional = totalNotional.div(config.gridCount);
  let qty = perOrderNotional.div(referencePrice);
  
  // Floor to base decimals
  const factor = new Decimal(10).pow(constraints.baseDecimalPlaces);
  qty = qty.mul(factor).floor().div(factor);
  
  // Enforce minimum
  if (qty.lt(constraints.minBaseAmount)) {
    throw new Error(
      `Computed qty ${qty} below minimum ${constraints.minBaseAmount}. ` +
      `Reduce gridCount or increase capital.`
    );
  }
  return qty;
}
```

Call this **at startup** AND **when free margin materially changes** (e.g. after every 10 fills — don't recompute on every tick).

## 6. Main loop (`src/app/main.ts`)

Pseudocode:

```
async function main() {
  1. Load + validate config
  2. Init REST client, pair metadata
  3. Cancel ALL existing orders on this pair (clean slate every startup)
  4. Close any existing position? NO — log warning, operate around it
     (future: configurable close_on_start)
  5. Fetch balance, current mark price
  6. Verify leverage tier matches config (call GET /v1/margin/leverage/{pair})
     - If mismatch: attempt PUT to set it. If still mismatch: halt with error.
  7. Build levels, compute initial quantityPerLevel
  8. Init SQLite store
  9. Connect WS price + account streams
  10. runId = timestamp when initialized
  11. Start reconciliation loop (every reconcileIntervalSecs)
  12. Start supervisor loop (every 10s)
  13. On SIGINT/SIGTERM: graceful shutdown (cancel all orders)
}

async function reconcileTick() {
  if (supervisor.isHalted()) return;
  
  const price = priceClient.getCurrentMarkPrice();
  if (price.isStale()) { logger.warn(...); return; }
  
  const inRange = price >= L && price <= U;
  if (!inRange) {
    supervisor.enterRangeExitState();
    return;
  }
  
  const desired = planDesiredOrders(levels, price, qtyPerLevel, runId, constraints);
  const exchangeOrders = await restClient.getOpenOrders(pair);
  const plan = reconcile(desired, exchangeOrders, runId);
  
  // Margin gate
  const freeBalance = await restClient.getFreeBalance('USDT');
  const estimatedMarginNeeded = plan.toPlace
    .map(o => o.price.mul(o.quantity).div(leverage))
    .reduce((a, b) => a.plus(b), new Decimal(0));
  
  let placements = plan.toPlace;
  if (freeBalance.lt(estimatedMarginNeeded)) {
    // Sort by distance to current price (nearest first), drop far-away
    placements.sort(byDistanceTo(price));
    // Trim until it fits
    while (placements.length > 0 && estimatedFor(placements).gt(freeBalance)) {
      placements.pop();  // drop the furthest
    }
    logger.warn({ dropped: plan.toPlace.length - placements.length }, 
                'margin-constrained batch');
  }
  
  // Execute cancels first
  await executeBatch(plan.toCancel.map(o => cancelOrder(o)), maxPlacementsPerSec);
  // Then placements
  await executeBatch(placements.map(o => placeOrder(o)), maxPlacementsPerSec);
  
  // Metrics
  store.updateMetrics({
    lastReconcileAt: now(),
    activeOrdersCount: desired.length - (plan.toPlace.length - placements.length),
    droppedDueToMargin: plan.toPlace.length - placements.length,
  });
}

// Event-driven: price update from WS triggers reconcile (throttled to 1/s)
priceClient.on('update', throttle(reconcileTick, 1000));

// Safety timer — always runs
setInterval(reconcileTick, config.reconcileIntervalSecs * 1000);
```

## 7. Supervisor (`src/app/supervisor.ts`)

Separate state machine. Runs its own loop every 10 seconds.

Responsibilities:

### Stop-loss
- Fetch open position, compute current loss from `averageEntryPrice`
- If `abs((currentPrice - avgEntry) / avgEntry) * 100 >= stopLossPercent` AND position is in losing direction:
  - Trigger `HALTED` state
  - Cancel all orders
  - Market-close position with `reduceOnly: true`
  - Alert (telegram + log)
  - Start cooldown timer

### Range exit
- If currentPrice < L or currentPrice > U:
  - Trigger `PAUSED` state
  - Cancel all orders
  - Keep monitoring price; resume when price back in range (if `onRangeExit: halt`)
  - Or close + reset (if `onRangeExit: close_and_reset`)

### Margin ratio
- Fetch margin info (`GET /v1/account/margin/futures` or similar)
- If `usedMargin / totalMargin > marginRatioAlertPercent`:
  - Alert once per 10 min
  - Do NOT halt

### Liquidation proximity
- If open position and `|liquidationPrice - currentPrice| / currentPrice < liquidationProximityPercent`:
  - Cancel ALL orders (both sides) to free margin
  - Alert loudly
  - Stay in alert state until margin ratio recovers

### Circuit breaker
- Track placement failures in a rolling window
- If `>= consecutiveFailuresThreshold` failures in `consecutiveFailuresWindowSecs`:
  - Trigger `HALTED` state
  - Alert
  - Auto-resume after `cooldownAfterStopSecs`

States: `RUNNING | PAUSED | HALTED | COOLDOWN`
Transitions logged.

## 8. Exchange client (`src/exchange/restClient.ts`)

HMAC-SHA512 signing per VALR docs.

Required endpoints:
- `GET /v1/account/balances` — with subaccount header
- `GET /v1/orders/open` — with subaccount header, filter client-side by pair
- `POST /v2/orders/limit` — place (supports postOnly, timeInForce, reduceOnly)
- `DELETE /v1/orders/{orderId}` — with `{ "pair": "..." }` body
- `POST /v1/orders/market` — for position close (reduceOnly, baseAmount)
- `GET /v1/positions/open` — position state
- `GET /v1/margin/leverage/{pair}` — current leverage tier
- `PUT /v1/margin/leverage/{pair}` — `{ "leverageMultiple": "10" }` (string!)
- `GET /v1/public/{pair}/marketsummary` — public, no auth needed (use for bootstrap mark price)
- `GET /v1/account/margin/futures` — margin info (find actual endpoint if the path differs)

**Critical:** 
- Subaccount ID goes in header `X-VALR-SUB-ACCOUNT-ID` **and** as the trailing string in the signature message (after body).
- `placeLimitOrder` must pass through `postOnly`, `reduceOnly`, `timeInForce` from caller. v3 dropped these silently — DO NOT repeat that bug.
- Body field for market close is `baseAmount`, not `quantity`.
- Order cancel needs body `{ "pair": "..." }`, not `{ "currencyPair": "..." }`.
- On HTTP 400 "Insufficient Balance": throw a typed error `InsufficientBalanceError`, don't retry.
- On HTTP 429: throw `RateLimitError` with `retryAfter` (ms). Caller decides whether to back off.
- Wrap all other errors as `ValrApiError(statusCode, message, path)`.

## 9. State store (`src/state/store.ts`)

SQLite via `better-sqlite3`. Schema:

```sql
CREATE TABLE orders (
  customer_order_id TEXT PRIMARY KEY,
  run_id            TEXT NOT NULL,
  level_index       INTEGER NOT NULL,
  side              TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
  price             TEXT NOT NULL,
  quantity          TEXT NOT NULL,
  exchange_order_id TEXT,
  state             TEXT NOT NULL CHECK(state IN ('desired','pending','active','filled','cancelled','rejected')),
  role              TEXT NOT NULL DEFAULT 'entry',
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  filled_at         TEXT,
  fill_price        TEXT
);

CREATE TABLE cycles (
  cycle_id          TEXT PRIMARY KEY,
  run_id            TEXT NOT NULL,
  entry_level       INTEGER NOT NULL,
  exit_level        INTEGER NOT NULL,
  entry_side        TEXT NOT NULL,
  entry_price       TEXT NOT NULL,
  exit_price        TEXT NOT NULL,
  quantity          TEXT NOT NULL,
  realised_profit   TEXT NOT NULL,
  completed_at      TEXT NOT NULL
);

CREATE TABLE metrics (
  key               TEXT PRIMARY KEY,
  value             TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);

CREATE INDEX idx_orders_state ON orders(state, updated_at);
CREATE INDEX idx_cycles_run ON cycles(run_id, completed_at);
```

**Auto-prune** on startup and every hour: delete rows from `orders` where `state IN ('cancelled','rejected')` older than 7 days, and `state='filled'` older than 30 days.

DB file: `./logs/{pair-lowercase}-state.db`

Metrics keys to track:
- `total_realised_pnl`
- `total_fills`
- `total_cycles`
- `last_reconcile_ts`
- `current_active_orders`
- `last_supervisor_state`
- `bot_started_at`

## 10. README.md (must be comprehensive)

Sections required:

1. **Banner** — one-liner description, badges (Node 22, MIT, "Built with OpenClaw")
2. **What this is** — plain-English explanation of grid trading, what the bot does, what it's good for, what it's NOT good for
3. **Quick start** (5 steps max):
   - Clone
   - `npm install && npm run build`
   - Copy `.env.example` → `.env`, add API keys
   - Copy `configs/sol.example.json` → `configs/sol.json`, set `pair`, `subaccountId`, `gridCount`, `lowerBound`, `upperBound`
   - `npm start -- configs/sol.json`
4. **Configuration** — full table of every config field with defaults + meaning
5. **Required inputs** — N, range, stop-loss (highlight these as the 3 decisions the user must make)
6. **Strategy explanation** — copy the spec from this file verbatim, add a diagram of a grid (ASCII art is fine)
7. **Risk management** — explain stop-loss, range exit, margin watch, circuit breaker
8. **Running as a service** — systemd instructions, log file locations
9. **Monitoring** — `npm run status -- configs/sol.json` to print current state
10. **Troubleshooting** — common issues: insufficient balance, stale price data, leverage tier mismatch
11. **How to add a new pair** — step-by-step: create config, set leverage tier on VALR, verify pair constraints, start service
12. **Architecture** — mermaid diagram or ASCII of module dependencies
13. **Development** — npm scripts, how to run tests (if any), how to contribute
14. **License** — MIT link
15. **Credits** — "Originally commissioned for VALR perpetual futures trading"

## 11. Alert system (`src/alerts/telegram.ts`)

OpenClaw exposes a local HTTP gateway for sending telegram messages. The bot should:

- Send a startup banner: pair, gridCount, range, leverage, stop-loss
- Send alerts on: stop-loss triggered, range exit, circuit breaker, liquidation warning, supervisor errors
- Send a daily summary: total cycles, total realised PnL, uptime
- NO alerts on routine fills (would be spammy)

Implementation: simple POST to `{telegramGatewayUrl}` with `{ chat_id, text }`. If `telegramGatewayUrl` not configured, skip silently.

## 12. Systemd template (`systemd/valr-perpetual-grid-bot@.service`)

```ini
[Unit]
Description=VALR Perpetual Grid Bot (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/admin/.openclaw/workspace/bots/valr-perpetual-grid-bot
EnvironmentFile=/home/admin/.openclaw/workspace/bots/valr-perpetual-grid-bot/.env
ExecStart=/usr/bin/node dist/app/main.js configs/%i.json
Restart=always
RestartSec=10
StandardOutput=append:%h/.openclaw/workspace/bots/valr-perpetual-grid-bot/logs/%i.log
StandardError=append:%h/.openclaw/workspace/bots/valr-perpetual-grid-bot/logs/%i.log

[Install]
WantedBy=default.target
```

User enables instance: `systemctl --user enable --now valr-perpetual-grid-bot@sol.service`
(with `configs/sol.json` existing)

`install.sh` script: copies unit file to `~/.config/systemd/user/`, runs `systemctl --user daemon-reload`.

## 13. Testing

- **Unit tests (Jest or vitest) for PURE functions:** `buildLevels`, `planDesiredOrders`, `reconcile`, `computeQuantityPerLevel`, stop-loss trigger logic.
- **Integration test:** boot with `dryRun: true` against a mock REST client, verify it generates the right `placeOrder` calls for a given price sequence.
- Don't need 100% coverage — hit the critical logic.

At minimum: 5 test files covering the 5 pure functions, 20+ test cases total.

## 14. Quality gates (don't skip these)

Before committing:
- `tsc --noEmit` must pass with ZERO errors
- `npm test` must pass
- `npm run build` must succeed
- Run `node dist/app/main.js configs/sol.example.json` with dryRun=true — must print desired grid without crashing

## 15. Git

- `git init` in `valr-perpetual-grid-bot/`
- `.gitignore`: `node_modules/`, `dist/`, `logs/`, `*.db`, `.env`, `configs/*.json` (EXCEPT `*.example.json`)
- Commit in logical chunks:
  1. Initial scaffold + package.json + tsconfig + README stub
  2. Types + config schema
  3. Pair metadata + REST client
  4. WS clients
  5. Strategy (grid, plan, reconciler, cycles)
  6. State store
  7. Supervisor + alerts
  8. Main + CLI scripts
  9. Systemd template + install.sh
  10. Tests
  11. Docs (README, LICENSE)
- Last commit message: "Initial release: v4 clean rewrite"
- Suggested remote: `git@github.com:Blake1863/valr-perpetual-grid-bot.git` (don't push — user will handle)
- Print the final `git log --oneline` and the `git remote add origin` command for the user in your final report

## 16. Configs to create (starting values)

`configs/sol.example.json`:
```json
{
  "pair": "SOLUSDTPERP",
  "subaccountId": "1432690254033137664",
  "gridCount": 30,
  "lowerBound": "82.00",
  "upperBound": "92.00",
  "stopLossPercent": 3.0,
  "gridMode": "geometric",
  "leverage": 10,
  "reservePercent": 10,
  "dryRun": false
}
```

`configs/eth.example.json`:
```json
{
  "pair": "ETHUSDTPERP",
  "subaccountId": "1491067064373735424",
  "gridCount": 30,
  "lowerBound": "2228.00",
  "upperBound": "2463.00",
  "stopLossPercent": 3.0,
  "gridMode": "geometric",
  "leverage": 10,
  "reservePercent": 10,
  "dryRun": false
}
```

## 17. Known context (for your awareness only — do NOT execute)

- The v3 bot had state drift (DB said 160 orders, exchange had 12), infinite retry on insufficient balance (65k fails/20MB log), stop-loss config ignored, dynamic bias broken.
- Both target subaccounts are fresh as of the time this spec was written: 0 open orders, 0 positions, ~$40 USDT each.
- SOL leverage was set to 10x on VALR (was 5x previously).
- The user's existing secrets are in `/home/admin/.openclaw/secrets/secrets.py`:
  - SOL subaccount uses `valr_grid_bot_1_api_key`/`valr_grid_bot_1_api_secret`
  - ETH subaccount uses `valr_main_api_key`/`valr_main_api_secret`
  - Your `.env.example` should reference these var names: `VALR_API_KEY`, `VALR_API_SECRET`.

## 18. Deliverables (report back)

When you're done, reply with:

1. **Summary** — one paragraph of what you built
2. **File tree** — output of `find valr-perpetual-grid-bot -type f | grep -v node_modules | sort`
3. **Test results** — output of `npm test`
4. **Build check** — confirm `tsc --noEmit` passes
5. **Dry-run proof** — run the bot in dry-run mode and paste the first 30 lines of output showing the grid was built
6. **Git log** — `git log --oneline`
7. **Next steps** — what the user needs to do to go live (create GitHub repo, push, create `configs/sol.json` from example, start service)
8. **Known limitations** — anything you couldn't finish or that needs follow-up

## 19. Constraints / hard rules

- Do NOT read or import from `/home/admin/.openclaw/workspace/bots/archived/valr-grid-bot-v3-2026-04-22/` — fresh start
- Do NOT touch anything outside `/home/admin/.openclaw/workspace/bots/valr-perpetual-grid-bot/`
- Do NOT run the bot against live API (except the dry-run step in §18) — user will do that themselves
- Do NOT create the GitHub remote yourself (no `gh` CLI, no git push) — just prepare local repo
- Do NOT modify systemd unit files in `~/.config/systemd/user/` — only ship the template file inside the repo
- If you hit a technical blocker (e.g. "I don't know the exact VALR margin endpoint"), **document it clearly** in the final report rather than guessing
- Preserve precision: always use `Decimal` from `decimal.js` for prices, quantities, margins — never native `number`
- Keep business logic (strategy, reconciliation, stop-loss math) as pure functions. All I/O isolated in `exchange/`, `state/`, `alerts/`.

## 20. Success criteria

✅ `tsc --noEmit` passes
✅ `npm test` passes
✅ `npm run build` produces working `dist/`
✅ Dry-run prints a valid 30-level grid for SOL config
✅ `README.md` is comprehensive and a new user could clone + configure + run in 10 minutes
✅ All 4 required user inputs (pair, N, range, stop-loss) are clearly documented
✅ Code is pushed to a local git repo (no remote yet)

---

**Begin. Report progress milestones as you go. Report final deliverables per §18 when done.**
