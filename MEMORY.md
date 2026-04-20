# MEMORY.md - Long-Term Memory

## Grid Bot Deployment Architecture (2026-04-07)

### ⚠️ CRITICAL: Account Structure

**SOLUSDTPERP bot does NOT run on the main account.**

Both grid bots run on **subaccounts**:

| Bot | Subaccount | ID |
|---|---|---|
| **SOLUSDTPERP** | Grid Bot 1 | `1432690254033137664` |
| **ETHUSDTPERP** | Grid Bot 2 | `1491067064373735424` |

### Why This Matters

1. **Futures must be enabled per-subaccount** — cannot assume main account status applies
2. **API key scoping** — primary API key can impersonate subaccounts via `X-VALR-SUB-ACCOUNT-ID` header
3. **Balance isolation** — each bot's capital is segregated to its subaccount
4. **Risk containment** — liquidation on one bot doesn't affect the other

### Current Configuration (Both Bots)

- **Leverage:** 10x
- **Levels:** 3 per side (6 total)
- **Spacing:** 0.4% (40 bps)
- **Dynamic Sizing:** ✅ Enabled (auto-adjusts with balance)
- **Capital Allocation:** 90%

### Funding State

- **Grid Bot 1 (SOL):** ~35 USDT (from initial deployment)
- **Grid Bot 2 (ETH):** 40 USDT (unlocked from DeFi lending + transferred)

### API Credentials

Primary API key (`primary account`) used for both bots via subaccount impersonation:
- Permissions: `View access`, `Trade`, `Internal Transfer`
- No separate API keys needed per subaccount

### Systemd Services

```bash
# SOL bot
systemctl --user status valr-grid-bot-v2.service

# ETH bot
systemctl --user status valr-grid-bot-v2-eth.service
```

Both services auto-restart on failure.

---

*Created: 2026-04-07 — Post-deployment correction*

---

## Grid Bot v3 — OKX/Bybit Style Neutral Grid (2026-04-19)

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
