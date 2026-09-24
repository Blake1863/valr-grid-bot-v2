# cm-bot-spot-py — Design Document (Phase 1)

**Status:** Phase 1 — Design only. No code committed yet. Awaiting operator review before Phase 2.

**Author:** main session (Opus), 2026-05-04.

**Replaces:** `cm-bot-spot.service` (Rust), `cm-bot-spot-illiquid.service` (Python), `cm-bot-spot-monitor.service` (Python). Three services → one.

**Does NOT touch:** `cm-bot-v2.service` (futures CM1/CM2, Rust). Stays as-is.

---

## 0. TL;DR for the operator

- New service: `valr-spot-bot.service`, single Python process, asyncio.
- Strategy: **same maker→taker pattern as the existing illiquid bot**, generalized to all 23 spot pairs (15 currently liquid + 8 illiquid). The illiquid bot is already correct from a leak-mitigation standpoint; the new bot is essentially that strategy, hardened, productionized, and given the inventory-management functions that the Rust bot and `monitor.py` currently provide.
- One config file, one state file, one log file, one systemd unit.
- Inventory floor + auto-rebalance + replenish-on-fail folded inline. The external `monitor.py` watcher and the `quote_replenish.py` / `spot_rebalance_manual.py` scripts become no longer required for steady-state running (kept as one-shot ops utilities).
- **Migration is per-pair, parallel run, dry-run gated.** No big-bang cutover.
- Strict logging discipline: INFO ≤ 1 line per cycle outcome, DEBUG behind env flag.

If you skim only one section after this, read **§4 (strategy decisions)** and **§7 (what's intentionally NOT being ported)**.

---

## 1. Leak-model recap (from BUILD_BRIEF §1)

The bot loses money on every cycle (small, by design — maker rebate + taker fee net out roughly to half a maker tier of fees per cycle). The job is to keep that loss as small and predictable as possible. The four leak modes:

| Code | Mode | Mitigation |
|------|------|------------|
| **A** | External taker hits our maker before our IOC fires | Minimize maker-placed → taker-fired latency; place maker as deep into the spread as physically possible (1 tick inside our own side, never crossing) |
| **B** | External maker improves price between our two orders | IOC limit at *exactly* maker's price, never market, never aggressive past it. If best opposite has improved past our maker, our IOC simply doesn't fill — no leak |
| **C** | Maker rejected (postOnly violation) | Skip cycle, don't fire taker, retry next interval with fresh book |
| **D** | Taker fills against external maker before reaching ours | VALR uses price-time priority. With IOC at exact maker price, if multiple makers exist at that price, we may consume an external one first. Mitigation: postOnly placement only at prices where there's a gap, so our maker is the *unique* resting order at that price level |

The illiquid bot already implements A, B, C, D correctly. The new bot inherits that pattern exactly.

**New addition: leak telemetry.** Every cycle is classified post-fact as `internal-fill / external-fill-A / no-fill-B-or-C / placement-failure`. The internal-fill ratio per pair becomes the primary health metric.

---

## 2. Inputs / sources

Read from the existing repo (do not duplicate):

- `bots/cm-bot-spot/.env` — credentials. Keys: `MAIN_API_KEY`, `MAIN_API_SECRET`, `CM1_SUBACCOUNT_ID`, `CM2_SUBACCOUNT_ID`. (Or `secrets.py get …` — pick one and stick with it; we'll go with `.env` to match the illiquid bot.)
- `skills/valr-exchange/references/valr-llms-full.txt` — local mirror of VALR API docs. **Source of truth. Never invent endpoints.**
- `bots/cm-bot-spot-illiquid/bot.py` — strategy template.
- `bots/cm-bot-spot/config.json` — current pair list (35 pairs, 15 enabled).
- `bots/cm-bot-spot-illiquid/config.json` — illiquid pair list (8 enabled).

Confirmed VALR endpoints we'll use (verified against the local mirror):

| Action | Endpoint | Notes |
|--------|----------|-------|
| Place limit | `POST /v1/orders/limit` | Body: pair/side/quantity/price/postOnly/customerOrderId/timeInForce |
| Cancel single | `DELETE /v1/orders/order` | Body: `{customerOrderId or orderId, pair}` (per illiquid bot) |
| Cancel all on subaccount | `DELETE /v1/orders` | Returns array of cancelled ids |
| Public orderbook | `GET /v1/public/{pair}/orderbook` | Aggregated, top of book sufficient |
| Public market summary | `GET /v1/public/{pair}/marketsummary` | bid/ask/last/mark, used as fallback if orderbook stale |
| Public pair metadata | `GET /v1/public/pairs` | tickSize, baseDecimalPlaces, minBaseAmount, minQuoteAmount |
| Account balances | `GET /v1/account/balances` | Per-subaccount via `X-VALR-SUB-ACCOUNT-ID` header |
| Sub-account transfer | `POST /v1/account/subaccounts/transfers` | For inventory rebalance |
| Order history detail | `GET /v1/orders/history/detail/customerorderid/{cid}` | Used to verify on transient HTML 400 |
| Public trade WS | `wss://api.valr.com/ws/trade` | Orderbook + market summary subscriptions |
| Account WS | `wss://api.valr.com/ws/account` | Per-subaccount, optional Phase 2.5 — see §6 |

(Memory.md's "🛑 CORE RULE" applies: every endpoint above must be cross-checked in the local docs mirror at implementation time, not assumed from this doc.)

---

## 3. Architecture

```
bots/cm-bot-spot-py/
  bot.py                       # asyncio entrypoint, top-level orchestration
  config.json                  # merged pair list, per-pair tuning, global knobs
  state.json                   # cycle counters, leak metrics, maker-side history
  pyproject.toml or requirements.txt
  README.md
  systemd/
    valr-spot-bot.service      # template, drop into ~/.config/systemd/user/
  src/
    config.py                  # config schema + load + validate
    creds.py                   # .env loader, secret access
    valr_rest.py               # signed REST: sign(), request(), wraps endpoints in §2
    valr_ws.py                 # public trade WS (orderbook subscriptions); account WS optional
    pair_meta.py               # pair metadata cache + tickSize/decimal helpers
    quoting.py                 # "where to place the maker inside the spread"
    sizing.py                  # qty computation given USD-value range + min_base + min_quote
    inventory.py               # balance fetch + floors + intra-bot rebalancer
    maker_selector.py          # randomized maker-account/side selection w/ inventory feedback
    cycle.py                   # one cycle of one pair: maker-place → taker-fire → cancel-maker → classify
    leak_monitor.py            # rolling window of internal/external fill ratios per pair, alerting
    metrics.py                 # in-memory counters, summary log emission
    logger.py                  # stdlib logging, INFO/DEBUG split, gated by BOT_DEBUG=1
    backoff.py                 # per-pair exponential backoff on placement failure
    orchestrator.py            # asyncio main loop: per-pair task, shared price feed, shared rate-limit
  tests/
    test_quoting.py
    test_sizing.py
    test_pair_meta.py
    test_maker_selector.py
    test_inventory_floor.py
    test_signing.py
    test_leak_monitor.py
  scripts/
    dry_run.py                 # invoke bot in dry-run mode
    one_off_rebalance.py       # human-triggered rebalance, replaces spot_rebalance_manual.py for ops use
```

### Concurrency

- Single Python process, asyncio.
- One coroutine task per enabled pair. Tasks share a global rate-limit semaphore (target: ≤80 req/sec across all pairs, well below VALR's ~150 req/sec ceiling, leaving headroom for the futures bot).
- Single shared public-WS connection, single shared price cache (`{pair: {bid, ask, ts, mark}}`).
- Each pair task runs its own configured cycle interval. No global tick.

### Why per-pair tasks (not single global tick)

- Allows different cycle cadences per pair without head-of-line blocking.
- Failure on one pair (postOnly reject, balance shortage, network blip) doesn't stall the others.
- Simpler back-off model — backoff lives on the pair task, not in a shared scheduler.

### Why not threads

- Python GIL + the workload is overwhelmingly I/O-bound (HTTP + WS).
- asyncio gives clean cancellation, structured concurrency via `asyncio.TaskGroup`, and aligns with `aiohttp` + `websockets`.

---

## 4. Strategy decisions (the §4 questions, answered)

### 4.1 Maker-account/side selection — **decision: randomized with two feedback loops**

Approach (port + extend the illiquid bot's logic):

1. **Random base.** 50/50 between CMS1 and CMS2 each cycle.
2. **History bias** (port of `random_maker.rs`). After 10 cycles, if one account has been the maker > 60% of the time, weight the next selection toward the other up to a 70/30 cap.
3. **Inventory feedback.** Before committing, check both accounts' base+quote inventory for the pair. If maker side would leave us under-floored on the asset they'd be selling, override the selection — flip to the other account, or flip the side (BUY ↔ SELL) on the chosen account.
4. **Hard cap.** Max 5 consecutive cycles with the same maker account on the same pair (matches Rust bot).

Maker side (BUY vs SELL) on each cycle alternates by a per-pair counter, so inventory sloshes both directions over time. This is what the illiquid bot does, and it's the right behavior — keeps inventory balanced inside each account too.

**Discarded:** the Rust bot's 6-cycle rotation (3 CMS1-sells then 3 CMS2-sells). The phase rotation doesn't help when pair cadences differ, and the random+bias model is simpler and yields the same long-run balance.

### 4.2 Maker price placement — **decision: 1 tick inside our side, skip cycle if no room**

Algorithm (port of illiquid bot's `cycle_pair`):

```
spread = best_ask - best_bid

# Refuse pathological spreads (broken markets)
if spread / mid > max_spread_bps / 10000: skip
# Refuse spreads with no room for postOnly placement
if spread < min_spread_ticks * tick: skip

if maker_side == SELL:
    maker_price = round_to_tick(best_ask - tick)
    if maker_price <= best_bid: skip  # tick math collapsed the gap
else:
    maker_price = round_to_tick(best_bid + tick)
    if maker_price >= best_ask: skip
```

`min_spread_ticks` defaults to 2 (need at least 1 tick of room above OUR side that's strictly inside the spread). For very illiquid pairs, can be tuned higher.

**Why not place at mid?** Mid is a frequent collision point with other algos and doesn't always round to a tick cleanly. 1-tick-inside-our-side is unambiguous and gives the maker a unique price level — the postOnly placement only succeeds if no resting order already sits there, so success itself confirms uniqueness (mitigates leak D).

**`max_spread_bps`:** default 200 (2%). If a pair's spread blows out past 2%, we suspect data is stale or market is broken; skip until it normalizes. Per-pair override allowed.

### 4.3 Quantity selection — **decision: USD-value range, with floors enforced**

```
value_usd = uniform(print_value_usd_min, print_value_usd_max)
qty = max(min_base, value_usd / maker_price)
qty = round_up_to_decimals(qty, baseDecimalPlaces)
notional = qty * maker_price
if notional < min_quote:
    qty = round_up((min_quote * 1.05) / maker_price)
```

Defaults per pair:
- Liquid ZAR pairs (BTCZAR/ETHZAR/etc): `print_value_usd_min=1.50, max=4.00`
- Liquid USDT pairs: same.
- Illiquid xStock pairs: `min=0.60, max=2.00` (per current illiquid config).
- Stablecoin pairs (EURCUSDC, USDCZAR): `min=0.80, max=2.50`.

(All overridable per pair in config.)

### 4.4 Internal vs external fill detection — **decision: per-cycle classification, rolling window, alert on threshold**

Cycle classification, evaluated immediately after the cancel-maker call returns:

| Maker outcome | Taker outcome | Cancel outcome | Classification |
|---------------|---------------|----------------|----------------|
| Filled | Filled (same price+qty, ~same trade id timing) | "order not found" | **internal-fill** |
| Filled | Not filled | "order not found" | **external-fill** (leak A) |
| Filled (partial) | Filled (partial) | succeeds (cancels remainder) | **internal-partial** (still mostly good) |
| Not filled / rejected | n/a (didn't fire) or unfilled | succeeds | **no-fill** (B or C, no leak) |
| HTML 400 / network error | unknown | unknown | **inconclusive** — verify via order history endpoint |

Rolling 100-cycle window per pair tracks `internal_fills / total_fills`. Health metric = that ratio. Defaults:

- `internal_fill_target`: 0.95 (we expect ≥95% internal on illiquid pairs, ≥80% on liquid).
- Alert threshold: drop below `internal_fill_floor` (default 0.80) for ≥10 cycles in a row → log WARN, optionally notify operator.
- Threshold breach also triggers a per-pair cooldown (skip for N cycles) to avoid bleeding into a hostile order book.

This is the **first time we'll have visibility into actual leakage**, and it's the single biggest improvement over the Rust bot.

### 4.5 Inventory floor — **decision: per-cycle preflight, hard skip below floor**

(Port of illiquid bot's preflight, applied to all pairs.)

For each cycle, before placing any order:
- Maker side needs `qty * 1.02` of base (if SELL) or `notional * 1.02` of quote (if BUY).
- Taker side needs the opposite.
- If either fails, **skip cycle** and increment `skipped_balance` counter.
- Soft warning if balance < per-pair `inventory_floor_usd`.

Hard floor + soft warn — the hard floor prevents sending obviously-doomed orders (eliminates the `Insufficient Balance` log spam that monitor.py was firefighting).

### 4.6 Auto-rebalance — **decision: lazy, asset-targeted, runs on demand**

Two-tier rebalance, replacing `monitor.py` + `quote_replenish.py` + `spot_rebalance_manual.py`:

**Tier 1 — Inline asset rebalance** (every 6 cycles per pair, configurable):
- For the pair's base + quote assets, fetch CMS1 and CMS2 balances.
- If one account holds > 60% of combined, transfer to make it 50/50.
- Skip if transfer value < `MIN_TRANSFER_VALUE_USD` (default $1) — don't burn API calls on dust.
- (Port of `rebalance.rs`.)

**Tier 2 — Failure-driven replenish** (folds in monitor.py logic):
- If a pair hits `skipped_balance` ≥ `replenish_failure_threshold` (default 3 in the last 10 cycles for that pair), trigger a quote-side replenish for that pair specifically.
- Quote replenish = sell some of the over-stocked base in the over-stocked account into ZAR/USDT, then transfer to the under-stocked account.
- Bounded: max one replenish action per pair per 30 minutes. Past that, log and stop trying — the operator should investigate.

This is more conservative than the current monitor.py (which fires both replenish + full rebalance on any 3-failure trigger). Less aggressive = less API churn = fewer cascading failures during volatile periods.

### 4.7 Cycle cadence per pair — **decision: per-pair, bucketed**

Defaults:
- **High-liquidity ZAR pairs** (BTC/ETH/SOL/XRP/BNB/AVAX/LINK ZAR): 12s
- **Stablecoin pairs** (EURCUSDC, USDCZAR, XAUTUSDT, XAUTZAR): 20s
- **Illiquid USDT pairs** (TSLAX/CRCLX/MSTRX/HOODX/etc): 20s (current)
- **Stocks-paired** (SPYXUSDT, NVDAXUSDT, COINXUSDT, TRUMPUSDT): 18s

Tunable per pair. Bucket assignments are config metadata, not hard-coded.

Stagger between pair starts: 1.5s (down from current 2s — 23 pairs at 2s = 46s of stagger which causes pair-23 to lag pair-1 by an entire cycle interval; 1.5s × 23 = 34.5s, still loose enough to avoid bursts).

### 4.8 Failure handling — **decision: per-pair exponential backoff, isolated**

- Maker rejected (postOnly violation): no backoff, just skip — this is normal.
- Network error / 5xx / 429: exponential backoff on **that pair only**, starting at 5s, capping at 60s, reset on success.
- WS disconnect: reconnect with jittered backoff (1s → 30s); during disconnect, fall back to REST orderbook polling (slower but functional).
- HTML 400 from VALR: verify via `GET /v1/orders/history/detail/customerorderid/{cid}` before deciding it failed. (Per MEMORY.md gotcha.)
- Unhandled exception in cycle: log, continue. Never let one pair's failure crash the whole bot.

---

## 5. Config schema (proposal)

```json
{
  "global": {
    "rate_limit_per_sec": 80,
    "summary_interval_seconds": 300,
    "rebalance_interval_cycles": 6,
    "rebalance_threshold_pct": 0.60,
    "min_transfer_value_usd": 1.00,
    "internal_fill_floor": 0.80,
    "internal_fill_window": 100,
    "internal_fill_alert_streak": 10,
    "replenish_failure_threshold": 3,
    "replenish_window_cycles": 10,
    "replenish_cooldown_seconds": 1800,
    "stagger_seconds": 1.5
  },
  "defaults": {
    "cycle_interval_seconds": 15,
    "min_spread_ticks": 2,
    "max_spread_bps": 200,
    "print_value_usd_min": 1.50,
    "print_value_usd_max": 4.00,
    "inventory_floor_usd": 5.00,
    "max_consecutive_same_maker": 5
  },
  "pairs": {
    "BTCZAR": { "enabled": true, "cycle_interval_seconds": 12 },
    "ETHZAR": { "enabled": true, "cycle_interval_seconds": 12 },
    ...
    "JUPUSDT": {
      "enabled": true,
      "cycle_interval_seconds": 20,
      "print_value_usd_min": 0.60,
      "print_value_usd_max": 2.00,
      "min_spread_ticks": 1
    },
    ...
  }
}
```

Per-pair overrides any default. `defaults` overrides any global default. Validate at startup; refuse unknown pair symbols, bad types, out-of-range values.

---

## 6. State, persistence, recovery

`state.json`:
```json
{
  "pairs": {
    "BTCZAR": {
      "cycle_count": 38827,
      "total_internal_fills": 35112,
      "total_external_fills": 1843,
      "total_no_fills": 1872,
      "maker_history": ["CMS1","CMS2","CMS1",...],   // last 10
      "consecutive_same_maker": 1,
      "last_replenish_ts": 1730727600,
      "leak_window": [1,1,1,0,1,...]  // last N classifications, 1=internal
    },
    ...
  },
  "schema_version": 1,
  "last_saved": "2026-05-04T20:00:00Z"
}
```

- Saved every cycle on the pair task that just completed (atomic write: write to `.tmp`, rename).
- On restart, replay state, drop any cycle counters > 24h old (assume drift), keep maker history for continuity.
- Schema version field for forward compatibility.

**Account WebSocket (Phase 2.5, optional):** the Rust bot uses account WS for low-latency placement and balance updates. The illiquid bot uses pure REST and works fine. **Initial Phase 2 implementation will be REST-only**, matching the illiquid bot, because:
- Latency between placement and IOC fire is dominated by network RTT (~30-50ms each) — REST is fine.
- WS auth + reconnection adds complexity and another failure mode.
- Balance updates are needed only every cycle (preflight), not real-time.

If post-rollout we observe leak rates above target (>20% external on liquid pairs), revisit account WS for placement. Until then, keep it simple.

---

## 7. What's intentionally NOT being ported

| From Rust bot | Why not ported |
|---------------|----------------|
| `liquidator.rs` | Edge-case ops tool; keep as standalone python script (`scripts/liquidate_spot.py` — already exists in `bots/`) |
| `random_maker.rs` 6-cycle phase rotation | Replaced by per-pair random+bias+inventory feedback (§4.1) — same outcome, simpler |
| Account WebSocket placement | Phase 2.5 only if needed (§6) |
| `state.rs` per-pair full state | Replaced by simplified state schema (§6) — keeps the meaningful counters, drops bookkeeping that didn't pay rent |
| `cleanup_interval_ms` periodic order sweeper | Replaced by per-cycle cancel-maker. The illiquid bot also does a `DELETE /v1/orders` between every cycle as a belt-and-suspenders sweep — we'll keep that |

| From monitor.py | Why not ported as separate service |
|----------------|------------------------------------|
| External log-tailing | Replaced by inline `replenish_failure_threshold` (§4.6). Not parsing log lines for state is fragile; checking failure counters in-process is reliable |
| `quote_replenish.py` external invocation | Folded into Tier 2 inline replenish (§4.6). Original script kept as a manual ops utility |
| `spot_rebalance_manual.py` external invocation | Same — keep as manual ops utility, automatic path is inline |

| From illiquid bot | Why kept |
|-------------------|----------|
| **Everything strategy-related is the model.** The pair-cycle, the maker→taker handshake, the cancel-maker safety net, the pre-cycle sub-account cancel-all sweep, the inventory preflight, the maker-side alternation, the pair_meta cache — all kept | Already correct; this is the "first principles" version we're scaling up |

---

## 8. Migration plan (Phase 3 detail) — ACCELERATED

Operator chose 15-min single-pair canary then full cutover (2026-05-04).

```
T+0:    Build complete. Dry-run 5 min on XRPZAR (intended-only, no orders).
T+5m:   Cutover XRPZAR live:
          - disable XRPZAR in bots/cm-bot-spot/config.json, restart cm-bot-spot
          - enable XRPZAR in bots/cm-bot-spot-py/config.json, start valr-cm-spot
T+20m:  Watch XRPZAR for 15 min. Decision gate:
          - internal-fill >= 90% over window     -> GO
          - internal-fill <  90% or any crash    -> ROLLBACK XRPZAR, abort
T+25m:  All-23 cutover:
          - disable all enabled pairs in cm-bot-spot/config.json
          - disable all pairs in cm-bot-spot-illiquid/config.json
          - enable remaining 22 pairs in cm-bot-spot-py/config.json
          - restart cm-bot-spot, cm-bot-spot-illiquid, valr-cm-spot
T+50m:  Health check across all 23 pairs. If healthy:
          systemctl --user stop cm-bot-spot.service
          systemctl --user stop cm-bot-spot-illiquid.service
          systemctl --user stop cm-bot-spot-monitor.service
          systemctl --user disable ...same...
T+24h:  If still healthy, archive bots/cm-bot-spot-src/, bots/cm-bot-spot/cm-bot-spot.bak-*,
        and bots/cm-bot-spot-illiquid/ to bots/archived/2026-05-XX/.
```

**Rollback at any step:** flip the pair(s) back on the old bot in their config, restart, disable on new bot. Both can run side-by-side per-pair-disjoint indefinitely.


## 9. Quality gates (Phase 2 acceptance)

Before requesting cutover:

- `pytest -q` passes. Tests cover: signing payload format, pair_meta parsing, tick-rounding, quoting decisions (including narrow-spread skip), sizing min-base/min-quote enforcement, maker-selector consecutive cap, inventory floor preflight, leak classifier on synthetic order outcomes.
- `pyflakes src/` clean. (mypy optional but encouraged.)
- `python bot.py --dry-run` for 5 minutes against live VALR public endpoints prints a sensible distribution of intended cycles across all enabled pairs, no exceptions.
- `python bot.py --selftest` runs a single live-but-dust-sized cycle on one ZAR pair (e.g. LINKZAR with $0.50 notional) end-to-end, prints internal-fill, exits 0.
- Service unit file installs cleanly: `systemctl --user daemon-reload && systemctl --user start valr-spot-bot.service && systemctl --user is-active …`
- Logrotate config covers the new log path (extend the existing `~/.config/logrotate/bot-logs.conf` to include `bots/cm-bot-spot-py/logs/*.log`).
- Memory file updated: add the new service to MEMORY.md and HEARTBEAT.md, mark old services as deprecated.

---

## 10. Operator decisions (locked in 2026-05-04)

1. **Service name:** TBD at Phase 2. Default: `valr-spot-bot.service`.
2. **Internal-fill alerting:** **TG alert when external-fill ratio ≥ 10% over the rolling window** (i.e. internal-fill ratio ≤ 90%). Sent via Telegram, throttled to one alert per pair per 30 min. Per-pair cooldown also kicks in (skip cycles for N intervals so we don't bleed into a hostile book).
3. **Account WS placement:** **Build into Phase 2 from day one.** Lower latency between maker placement and IOC fire = directly reduces leak A. Adds complexity but justified given §1 leak modes.
4. **xStocks / pair list:** **All 23 pairs stay enabled** (15 liquid currently in Rust + 8 illiquid currently in Python). New bot owns all of them after migration.
5. **Phase 2 author:** Subagent allowlist setup deferred (requires gateway restart, would interrupt flow). Main session (Opus) writes Phase 2 in-line. Most strategy decisions are locked, illiquid bot is most of the cycle code, so the marginal cost vs. spawning out a sonnet is small.

### Knock-on changes from the locked decisions

- §4.4 internal-fill threshold: change `internal_fill_floor` default from 0.80 to **0.90**. Alert streak threshold drops from 10 cycles to **a single rolling-window evaluation crossing the line** (window = 100 cycles).
- §6: account WS is now **Phase 2 core**, not Phase 2.5. Per-subaccount account WS connection, `PLACE_LIMIT_WS` for both maker and taker placement, REST as fallback only on WS disconnect.
- §3 architecture: add `valr_account_ws.py` module (separate from `valr_ws.py` which is public-trade-only).
- Telegram alerter: new module `alerter.py` posting to operator's Telegram via OpenClaw `message` tool / direct bot API. Phase 2 will pick the simplest viable path.

---

## 11. Risk register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Python latency causes higher external-fill leak vs Rust | Medium | Low | Phase 2.5 account-WS placement option; per-pair leak monitor catches it within hours |
| Migration breaks a pair mid-cutover | Low | Medium | Per-pair migration with side-by-side runs; instant rollback |
| Subaccount transfer endpoint changes | Low | Low | Verify against local docs mirror at impl time; transfer is non-critical (skip and warn if it fails) |
| State.json corruption on crash | Low | Low | Atomic write, fall back to fresh state with maker_history reset only |
| VALR rate limit during burst (rebalance + cycles) | Low | Low | Global rate-limit semaphore, headroom for futures bot |
| postOnly rejection cascade on a pair | Medium | Low | Already idempotent — postOnly reject = skip cycle, no state change |
| Operator (you) accidentally enables both old and new bot for the same pair | Medium | High | Startup check: if pair is enabled in both `cm-bot-spot/config.json` *and* `cm-bot-spot-py/config.json`, refuse to start with explicit error |

---

## 12. Files this doc commits to writing in Phase 2

- `bots/cm-bot-spot-py/bot.py`
- `bots/cm-bot-spot-py/src/{config,creds,valr_rest,valr_ws,pair_meta,quoting,sizing,inventory,maker_selector,cycle,leak_monitor,metrics,logger,backoff,orchestrator}.py`
- `bots/cm-bot-spot-py/tests/test_*.py` (≥7 test files per §9)
- `bots/cm-bot-spot-py/config.json`, `state.json` (empty initial)
- `bots/cm-bot-spot-py/systemd/valr-spot-bot.service`
- `bots/cm-bot-spot-py/scripts/dry_run.py`, `scripts/one_off_rebalance.py`
- `bots/cm-bot-spot-py/README.md`
- `bots/cm-bot-spot-py/ROLLOUT.md`

Documentation updates (deferred to Phase 3 cutover):
- `MEMORY.md` — add new service, mark old as deprecated
- `HEARTBEAT.md` — replace cm-bot-spot block with valr-spot-bot block
- `~/.config/logrotate/bot-logs.conf` — extend to cover new log path

---

**End of Phase 1.** Awaiting operator decisions on §10 before starting Phase 2.
