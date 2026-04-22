# MEMORY.md - Long-Term Memory

## Grid Bot v4 — PERPETUAL BOT (2026-04-22) 🆕

**Status:** Built, tested, NOT YET DEPLOYED. Awaiting user go-live.

**Location:** `bots/valr-perpetual-grid-bot/` — standalone git repo, clean history

**Quality gates (all pass):**
- ✅ `tsc --noEmit` — zero errors
- ✅ `npm test` — 32 tests across 5 suites (grid, plan, reconciler, sizing, cycles)
- ✅ `npm run build` — produces working `dist/`
- ✅ Dry-run prints valid 30-level SOL grid (15 BUY + 15 SELL, geometric)

**Known v3 bugs FIXED in v4:**
- postOnly/reduceOnly/timeInForce now pass through restClient (v3 dropped them)
- Cancel uses `{pair}` body, not `{currencyPair}`
- Market close uses `baseAmount`, not `quantity`
- Dynamic inventory bias: plan.ts recomputes BUY/SELL every tick from (level_price, current_price)
- Stop-loss: config-driven, percent from `averageEntryPrice`, default 3%, triggers cancel-all + market close
- Range exit: HALT mode (cancel all, pause, wait for re-entry) — no more infinite retry on insufficient margin
- Reconciliation: pure-function diff(desired, exchangeTruth) — no more state drift

**Config — only 4 required user inputs:**
- `pair`, `subaccountId`, `gridCount` (N), `lowerBound`/`upperBound` (range)
- `stopLossPercent` defaults to 3.0
- Everything else has sensible defaults

**Known limitations (per subagent report):**
1. State = JSON file, not SQLite (better-sqlite3 build timed out; migration path documented)
2. decimal.js loaded via `createRequire` shim (ESM typing workaround)
3. WS subscription format is approximate — may need tweaking vs actual VALR WS docs
4. `/v1/account/margin/futures` endpoint path may differ
5. `cancelAllOrders` uses a batch endpoint — fallback = iterate openOrders if that endpoint doesn't exist

**GitHub:** Repo NOT pushed yet. User needs to:
```bash
# on github.com: create public repo Blake1863/valr-perpetual-grid-bot
cd bots/valr-perpetual-grid-bot
git remote add origin git@github.com:Blake1863/valr-perpetual-grid-bot.git
git push -u origin master
```

**Spec file:** `BUILD_SPEC_valr_perpetual.md` (688 lines, in workspace root) — keep for reference.

---

## Grid Bot Versions — Deprecation Status (2026-04-21)

### ⚠️ ARCHIVED: Grid Bot v3 — OKX/Bybit Style Neutral Grid

**Status:** ARCHIVED 2026-04-22 — replaced by v4 (see top of file)

**Services:**
- `valr-grid-bot-v3.service` (SOLUSDTPERP)
- `valr-grid-bot-v3-eth.service` (ETHUSDTPERP)

**Subaccounts:**
| Bot | Subaccount | ID |
|-----|------------|----|
| SOL | Grid Bot 1 | `1432690254033137664` |
| ETH | Grid Bot 2 | `1491067064373735424` |

**Configuration:**
| Parameter | SOL | ETH |
|-----------|-----|-----|
| Pair | SOLUSDTPERP | ETHUSDTPERP |
| Range | $82–$92 | $2228–$2463 |
| Grid Count | 30 intervals | 30 intervals |
| Grid Mode | Arithmetic | Arithmetic |
| Reference | $86.00 | $2345.50 |
| Leverage | 10x | 10x |
| Capital Alloc | 100% | 100% |
| Dynamic Sizing | ✅ | ✅ |
| Stop Loss | 3% | 3% |

**API Credentials:** `primary account` key with subaccount impersonation

---

### ⚠️ DEPRECATED: Grid Bot v3 — Archived 2026-04-22

**Status:** ARCHIVED — moved to `bots/archived/valr-grid-bot-v3-2026-04-22/`

**Services:**
- `valr-grid-bot-v3.service` — stopped, disabled (file still exists in `~/.config/systemd/user/`, can be removed)
- `valr-grid-bot-v3-eth.service` — stopped, disabled

**Replacement:** v4 (see above)

**Why retired:** 4 critical bugs + state drift + infinite retry on insufficient margin (65k failed orders logged in ~20MB window). See v4 section for fixes.

---

### ⚠️ DEPRECATED: Grid Bot v2 (SOL + ETH)

**Status:** DEPRECATED — Stopped and disabled

**Services:**
- `valr-grid-bot-v2.service` — STOPPED
- `valr-grid-bot-v2-eth.service` — STOPPED

**Why Deprecated:**
- Replaced by v3's OKX/Bybit-style grid mechanics
- v3 has superior cycle tracking, range management, and state persistence
- v2 uses simpler linear grid model without geometric mode support

**Migration:** ✅ COMPLETE — Both SOL and ETH migrated to v3

---

### ⚠️ DEPRECATED: Grid Bot v1 (Original Rust Bot)

**Status:** DEPRECATED — Stopped and disabled

**Service:** `valr-grid-bot.service` — STOPPED

**Why Deprecated:**
- Original Rust implementation, superseded by TypeScript versions
- Limited feature set compared to v2/v3
- No active development

---

## Grid Bot v3 — Architecture Details

### Architecture Overview

**Completely rewritten** to replicate OKX/Bybit neutral futures grid mechanics.

| Feature | v2 | v3 |
|---------|----|----|
| Grid model | N total orders | N intervals (OKX/Bybit convention) |
| Grid modes | Linear only | Arithmetic + Geometric |
| Neutral mode | Approximate | Exact OKX/Bybit replica |
| Cycle tracking | Basic | Per-cycle profit accounting |
| Range exit | Continue | Pause new entries |
| State persistence | Minimal | Full SQLite persistence |

### Key Concepts

**Grid Range:** Fixed bounds `[lowerBound, upperBound]` — stops placing new entries when price exits, resumes on re-entry.

**Grid Construction:**
- `gridCount` = intervals (not levels) — matches OKX/Bybit
- `gridMode`: `arithmetic` (equal price diff) or `geometric` (equal ratio)

**Neutral Mode Logic:**
- Below reference price → BUY orders
- Above reference price → SELL orders
- Adjacent-level cycles: Buy at L[i] → Sell at L[i+1], Sell at L[i] → Buy at L[i-1]

**PnL Tracking:**
- Realized profit per completed grid cycle
- Unrealized PnL tracked separately
- Fee-aware cycle profit calculation

### State Persistence

SQLite databases:
- `logs/solusdtperp-state.db` (SOL bot)
- `logs/ethusdtperp-state.db` (ETH bot)

Query completed cycles:
```sql
SELECT * FROM cycles ORDER BY completedAt DESC LIMIT 10;
SELECT SUM(realizedProfit) FROM cycles;
```

### Systemd Services

```bash
# SOL bot
systemctl --user status valr-grid-bot-v3.service

# ETH bot
systemctl --user status valr-grid-bot-v3-eth.service
```

Both auto-restart on failure.

### Safety Features

- `dryRun`: Test without real orders
- `postOnly`: Maker-only (no taker fees)
- `staleDataTimeoutMs`: Pause if price data stale (30s default)
- `maxActiveGridOrders`: Limit concurrent orders
- `wsStaleTimeoutSecs`: WebSocket health monitoring

---

*Created: 2026-04-21 — v3 deployment notes*
*Updated: 2026-04-21 — ETH migrated to v3, v1/v2 deprecated*

---

## Bot Log Management (2026-04-22)

**Unified logrotate** covers all active bot logs with **7-day TTL**:
- Config: `~/.config/logrotate/bot-logs.conf`
- Service + timer: `cm-bot-logrotate.{service,timer}` (daily, +15min jitter, persistent)
- State: `~/.config/logrotate/bot-logs.state`
- Strategy: `copytruncate` (safe for pino fileStream, systemd append, python tail -F)
- Triggers: daily OR size > 200MB, keeps 7 compressed rotations

**Covered paths** (all via wildcard):
- `bots/cm-bot-spot/logs/*.log` — wash trading bot (CMS1/CMS2)
- `bots/cm-bot-v2/logs/*.log` — futures offset bot
- `bots/valr-grid-bot-v3/logs/*.log` — grid bots (SOL + ETH)

**Quarantine auto-purge:** `~/.openclaw/workspace/.log-quarantine/YYYY-MM-DD/` — deleted after 14 days by the same timer.

**Restored `cm-bot-spot-monitor.service`** (auto-replenish) — had been dead since Apr 16. Triggers `quote_replenish.py` + `spot_rebalance_manual.py` after 3 consecutive Insufficient Balance failures on any pair.

**Known issue to follow up:** grid-v3 logs every `Insufficient Balance` at `level:50` with full error payload — generates most of the bot.log volume. Consider dedupe/throttle in `src/app/logger.ts` or raise HTTP-error level in the REST client.

**Heartbeat checks** added to `HEARTBEAT.md` — weekly log sanity, daily grid + wash bot health.

