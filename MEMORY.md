# MEMORY.md - Long-Term Memory

## Grid Bot Versions — Deprecation Status (2026-04-21)

### ✅ CURRENT: Grid Bot v3 — OKX/Bybit Style Neutral Grid

**Status:** ACTIVE — Primary production bot

**Service:** `valr-grid-bot-v3.service`

**Subaccount:** Grid Bot 1 (`1432690254033137664`)

**Pair:** SOLUSDTPERP

---

### ⚠️ DEPRECATED: Grid Bot v2 (SOL + ETH)

**Status:** DEPRECATED — Running but scheduled for shutdown

**Services:**
- `valr-grid-bot-v2.service` (SOLUSDTPERP)
- `valr-grid-bot-v2-eth.service` (ETHUSDTPERP)

**Subaccounts:**
| Bot | Subaccount | ID |
|---|---|---|
| SOL | Grid Bot 1 | `1432690254033137664` |
| ETH | Grid Bot 2 | `1491067064373735424` |

**Why Deprecated:**
- Replaced by v3's OKX/Bybit-style grid mechanics
- v3 has superior cycle tracking, range management, and state persistence
- v2 uses simpler linear grid model without geometric mode support

**Migration:** SOL bot migrated to v3. ETH bot can be migrated when needed.

---

### ⚠️ DEPRECATED: Grid Bot v1 (Original Rust Bot)

**Status:** DEPRECATED — Legacy, should be stopped

**Service:** `valr-grid-bot.service`

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

### Current Configuration (SOLUSDTPERP)

```json
{
  "pair": "SOLUSDTPERP",
  "subaccountId": "1432690254033137664",
  "mode": "neutral",
  "lowerBound": "82.00",
  "upperBound": "92.00",
  "gridCount": 30,
  "gridMode": "arithmetic",
  "referencePrice": "86.00",
  "leverage": 10,
  "capitalAllocationPercent": 100,
  "dynamicSizing": true,
  "quantityPerLevel": "0.165",
  "stopLossMode": "percent",
  "stopLossValue": "3.0",
  "postOnly": true,
  "dryRun": false
}
```

### State Persistence

SQLite database: `logs/solusdtperp-state.db`

Query completed cycles:
```sql
SELECT * FROM cycles ORDER BY completedAt DESC LIMIT 10;
SELECT SUM(realizedProfit) FROM cycles;
```

### Systemd Service

```bash
systemctl --user status valr-grid-bot-v3.service
```

Auto-restarts on failure.

### Safety Features

- `dryRun`: Test without real orders
- `postOnly`: Maker-only (no taker fees)
- `staleDataTimeoutMs`: Pause if price data stale (30s default)
- `maxActiveGridOrders`: Limit concurrent orders
- `wsStaleTimeoutSecs`: WebSocket health monitoring

---

*Created: 2026-04-21 — v3 deployment notes*
