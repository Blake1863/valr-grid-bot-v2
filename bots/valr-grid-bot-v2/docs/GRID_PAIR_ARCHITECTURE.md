# Symmetric Grid Bot Architecture

## Overview

This document describes the improved symmetric grid bot implementation with explicit pair tracking. The bot maintains a clean, symmetric grid structure within a defined level range, replacing completed pairs to keep the grid full.

## Core Principles

### 1. Grid as Explicit Pairs

The grid is no longer tracked as loose independent orders. Instead, it's structured as **N pairs**, where each pair contains:
- One bid leg (BUY order below reference price)
- One ask leg (SELL order above reference price)

```
Pair 1: Bid @ R*(1-s)¹  |  Ask @ R*(1+s)¹
Pair 2: Bid @ R*(1-s)²  |  Ask @ R*(1+s)²
Pair 3: Bid @ R*(1-s)³  |  Ask @ R*(1+s)³
```

### 2. Fixed Level Range

The grid **never expands beyond configured levels**. With `levels: 3`:
- Exactly 3 bid levels (1, 2, 3)
- Exactly 3 ask levels (1, 2, 3)
- No level 4, 5, 6, etc. — ever

This prevents:
- Unlimited exposure from repeated fills in one direction
- Directional bias from asymmetric replenishment
- Capital exhaustion from expanding grid

### 3. Pair Replacement (Not Expansion)

When a pair completes (both legs filled):
1. The pair is marked as `complete`
2. A **new pair is created at the same level**
3. Both legs are placed as fresh orders

This keeps the grid "full" within the configured range.

### 4. Natural Exposure Allowed

The bot **may** become net long or net short as price trades through the grid. This is normal and expected. The key constraint is:
- Replenishment logic must not introduce **artificial** directional bias
- Grid structure remains symmetric even when exposure is asymmetric

## Pair States

Each pair transitions through these states:

```
missing → active → partial → complete → (replaced) → missing
                ↓
            (one leg filled)
```

| State | Description |
|-------|-------------|
| `missing` | Neither leg placed |
| `active` | Both legs placed, neither filled |
| `partial` | One leg filled, one leg still active |
| `complete` | Both legs filled (pair cycle done) |

## Grid State Tracking

The `GridState` object tracks:
- All pairs with their current states
- Reference price used for level calculation
- Statistics: active bids/asks, partial/complete/missing pairs

```typescript
interface GridState {
  pairs: GridPair[];
  referencePrice: Decimal;
  totalActiveBids: number;
  totalActiveAsks: number;
  partialPairs: number;
  completePairs: number;
  missingPairs: number;
}
```

## Key Functions

### `buildGridState()`
Creates initial grid with all pairs in `missing` state.

### `markLegActive()`
Called when an order is confirmed by exchange. Updates pair state.

### `markLegFilled()`
Called when WS reports a fill. Returns the affected pair for replenishment logic.

### `getCompletedPairs()`
Returns pairs where both legs have filled. These need replacement.

### `replaceCompletedPair()`
Creates a fresh pair at the same level, resetting both legs to `missing`.

### `needsRecenter()`
Checks if price has drifted beyond threshold (50% of grid range). Triggers rebuild.

### `rebuildGridWithNewReference()`
Recalculates all levels around new reference price. Preserves filled legs (open exposure).

## Replenishment Flow

```
1. Fill event received via WS
   ↓
2. markLegFilled() → pair state updated
   ↓
3. getCompletedPairs() → find pairs to replace
   ↓
4. For each completed pair:
   - replaceCompletedPair() → fresh pair at same level
   - placeSingleOrder() → place bid leg
   - placeSingleOrder() → place ask leg
   ↓
5. Grid is full again (N bids + N asks)
```

## Recenter Policy

The grid rebuilds around a new reference price when:
- Price move > 50% of grid range

Example with 3 levels, 0.4% spacing:
- Grid range: ~98.80 to ~101.20 (range ≈ 2.40)
- Threshold: 2.40 × 0.5 = 1.20
- Recenter triggers if price moves > $1.20 from original reference

When rebuilding:
- Filled legs are preserved (we keep the exposure)
- Missing/active legs are recalculated at new prices
- Grid structure remains symmetric

## Safety Behaviors

### Stale Price Detection
- WebSocket price data checked every 30s
- If stale: pause new entries, preserve protective orders

### Circuit Breaker
- 3 consecutive order failures → halt placements for 60s
- Prevents cascading failures during exchange issues

### Cooldown After Stop
- Position closed by stop-loss → 5 minute cooldown
- Prevents immediate re-entry into same conditions

### Startup Reconciliation
- Exchange state is authoritative
- Restore grid orders from previous run (matched by customerOrderId prefix)
- Cancel orphaned orders (unknown customerOrderIds)
- Detect and fix asymmetric grids from stale restarts

## Configuration

```json
{
  "mode": "neutral",
  "levels": 3,
  "spacingMode": "percent",
  "spacingValue": "0.4",
  "dynamicSizing": true,
  "targetLeverage": 10,
  "capitalAllocationPercent": 90,
  "stopLossMode": "percent",
  "stopLossValue": "3.0",
  "referencePriceSource": "mark_price"
}
```

### Key Parameters

| Parameter | Effect |
|-----------|--------|
| `levels` | Number of pairs (N bids + N asks) |
| `spacingValue` | % between levels (compound) |
| `dynamicSizing` | Auto-adjust qty with balance |
| `targetLeverage` | Effective leverage for sizing |
| `stopLossValue` | SL distance from entry |

## Observability

Structured logs explain:
- Grid state on startup and after each event
- Why a pair was considered complete
- Why the grid was rebuilt
- Why an order was cancelled/replaced/suppressed
- Current net exposure vs. grid structure
- Protective stop coverage

Example log output:
```
INFO Grid state summary: {
  referencePrice: "100.00",
  totalActiveBids: 3,
  totalActiveAsks: 3,
  partialPairs: 0,
  completePairs: 0,
  missingPairs: 0,
  targetLevels: 3,
  isFull: true
}

INFO Completed pair replaced — grid restored to full: {
  pairId: "pair-1",
  level: 1,
  completedAt: "2026-04-19T03:15:00.000Z"
}

INFO Grid recenter triggered — price moved beyond threshold: {
  oldRef: "100.00",
  newRef: "102.50",
  priceMove: "2.50",
  threshold: "1.20",
  gridRange: "2.40"
}
```

## Testing

Comprehensive tests cover:
- ✅ Symmetric grid generation
- ✅ Pair state transitions
- ✅ Pair replacement logic
- ✅ Grid stays within configured range
- ✅ Partial fills handling
- ✅ Recenter trigger on price drift
- ✅ Dynamic quantity calculation
- ✅ Exposure preservation during rebuild

Run tests:
```bash
npm test -- --run gridPairManager
```

## Migration Notes

### What Changed
- Grid now tracked as explicit pairs, not loose orders
- Replenishment replaces completed pairs (no level expansion)
- Recenter policy triggers rebuild on significant price drift
- Enhanced observability with structured grid state logging

### What Stayed the Same
- WebSocket + REST client architecture
- SQLite state persistence
- HMAC-SHA512 authentication
- systemd process management
- Subaccount isolation
- Stop-loss via conditional orders

### Backward Compatibility
- Existing config files work unchanged
- Database schema extended (pairId field added)
- Legacy directional modes (long_only/short_only) still use original logic

## Future Improvements

Potential enhancements:
- Asymmetric grid support (different N for bids vs asks)
- Dynamic spacing based on volatility
- Grid profile switching (conservative ↔ aggressive)
- Backtest integration for parameter optimization
- Multi-pair grid coordination
