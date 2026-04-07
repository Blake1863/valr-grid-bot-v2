# SOLUSDT Grid Configuration

**Last Updated:** 2026-03-27 01:05 UTC  
**Mode:** Base currency sizing (fixed SOL per order)  
**Goal:** Neutral grid — no directional bias

---

## Current Configuration

| Parameter | Value |
|-----------|-------|
| Pair | SOLUSDT |
| Position | Short 1.1 SOL @ $87.71 |
| Mark Price | $87.33 |
| Unrealized PnL | +$0.42 |

### Grid Levels (Symmetric)

| Side | Price | Qty | USDT Value |
|------|-------|-----|------------|
| **Buy 1** | $86.32 | 1.1 SOL | $94.95 |
| **Buy 2** | $85.97 | 1.1 SOL | $94.57 |
| **Buy 3** | $85.62 | 1.1 SOL | $94.18 |
| **Buy 4** | $85.28 | 1.1 SOL | $93.81 |
| **Sell 1** | $88.06 | 1.1 SOL | $96.87 |
| **Sell 2** | $88.41 | 1.1 SOL | $97.25 |
| **Sell 3** | $88.76 | 1.1 SOL | $97.64 |

### Grid Stats
- **Spacing:** ~$0.35 per level (0.4%)
- **Total Buy Qty:** 4.4 SOL
- **Total Sell Qty:** 3.3 SOL
- **Net Bias:** +1.1 SOL (matches short position = **neutral**)

---

## Why This Works

**Before (quote sizing — BROKEN):**
- Fixed ~$100 USDT per order
- $100 @ $85 = 1.176 SOL (buy)
- $100 @ $88 = 1.136 SOL (sell)
- **Result:** Accumulate more SOL on dips than distribute on rips = long bias

**After (base sizing — CORRECT):**
- Fixed 1.1 SOL per order
- 1.1 SOL @ $85 = $93.50 (buy)
- 1.1 SOL @ $88 = $96.80 (sell)
- **Result:** Symmetric fills, grid profits from volatility only

**Position hedge:**
- Short 1.1 SOL position = natural hedge against the extra buy level
- If price drops and all 4 buys fill: +4.4 SOL
- If price rips and all 3 sells fill: -3.3 SOL (plus -1.1 from short = -4.4 SOL)
- **Net: Perfectly balanced**

---

## Rebalance History

| Date | Action | Reason |
|------|--------|--------|
| 2026-03-27 01:05 UTC | Rebalanced to base sizing | Grid was imbalanced (buys > sells) due to quote-currency sizing |

### Previous Config (Quote Sizing — DELETED)
```
Buys:  1.2 SOL @ 85.28, 85.62, 85.97, 86.32  (4.8 SOL total)
Sells: 1.1 SOL @ 88.06, 88.41, 88.76         (3.3 SOL total)
Imbalance: +1.5 SOL long bias ❌
```

### New Config (Base Sizing — ACTIVE)
```
Buys:  1.1 SOL @ 85.28, 85.62, 85.97, 86.32  (4.4 SOL total)
Sells: 1.1 SOL @ 88.06, 88.41, 88.76         (3.3 SOL total)
Position: Short 1.1 SOL
Net: 4.4 - 3.3 - 1.1 = 0 ✅ NEUTRAL
```

---

## Risk Parameters

| Parameter | Value |
|-----------|-------|
| Max grid exposure | ~$380 (4.4 SOL buys) |
| Stop Loss | $90.34 (triggered if price rips) |
| Take Profit | None (grid captures range) |
| Leverage | 5x |
| Account equity | ~$110 |
| Grid as % of equity | ~345% (aggressive, but hedged) |

---

## Monitoring

- **Cron:** `*/5 * * * *` — monitor.py checks position every 5 min
- **Log:** `bots/bybit-trading/logs/monitor.log`
- **Alerts:** None yet (TODO: add Telegram alerts for grid fills)

---

## Rules

1. **Never change order sizes** — keep 1.1 SOL fixed
2. **Adjust prices only** — if range shifts, move all levels proportionally
3. **Watch the short position** — if SL hits, grid becomes long-only (re-evaluate)
4. **Document any changes** — update this file when config changes
