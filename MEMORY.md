# MEMORY.md - Long-Term Memory

## Grid Bot Versions — Deprecation Status (2026-04-21)

### ✅ CURRENT: Grid Bot v3 — OKX/Bybit Style Neutral Grid

**Status:** ACTIVE — Primary production bot

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
