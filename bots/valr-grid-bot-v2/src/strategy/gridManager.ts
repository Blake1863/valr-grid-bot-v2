/**
 * Bounded Price-Range Grid Manager
 * 
 * Core behavior:
 * - Fixed price band: [lowerBound, upperBound]
 * - Exactly N live resting entry orders (total, not per side)
 * - Bid/ask split varies naturally with price position
 * - Deterministic level selection (nearest to reference price)
 * - Never place orders outside the configured range
 * 
 * Mental model:
 * "Maintain N live resting entry orders drawn from valid levels inside the range"
 */

import Decimal from 'decimal.js';
import type { BotConfig } from '../config/schema.js';
import type { PairConstraints } from '../exchange/pairMetadata.js';
import { priceToString, qtyToString } from '../exchange/pairMetadata.js';
import { createLogger } from '../app/logger.js';

const log = createLogger('gridManager');

export type LegState = 'missing' | 'pending' | 'active' | 'filled' | 'cancelled';

export interface GridLevel {
  levelIndex: number;         // 1, 2, 3... (sorted by distance from range center)
  price: Decimal;
  priceStr: string;
  side: 'BUY' | 'SELL';
  customerOrderId: string;
  exchangeOrderId?: string;
  state: LegState;
  quantity: Decimal;
  quantityStr: string;
  distanceFromRef: Decimal;   // |price - referencePrice|
  distanceFromCenter: Decimal; // |price - rangeCenter|
  filledAt?: string;
}

export interface GridState {
  levels: GridLevel[];        // All valid levels inside [lowerBound, upperBound]
  lowerBound: Decimal;
  upperBound: Decimal;
  rangeCenter: Decimal;
  referencePrice: Decimal;
  currentPrice: Decimal;
  activeLevels: GridLevel[];  // Exactly N levels selected for placement
  activeBidCount: number;
  activeAskCount: number;
}

/** Build customerOrderId — max 50 chars */
function buildCOID(side: 'BUY' | 'SELL', level: number, seed: string): string {
  const s = side === 'BUY' ? 'B' : 'S';
  return `grid-${s}-${level}-${seed}`.slice(0, 50);
}

/**
 * Compute spacing per level from range and level count.
 * Linear spacing for predictable, unique levels.
 */
function computeSpacing(lowerBound: Decimal, upperBound: Decimal, levels: number): Decimal {
  return upperBound.minus(lowerBound).div(levels - 1);
}

/**
 * Calculate quantity per level from available balance.
 */
export function calculateQuantityPerLevel(
  config: BotConfig,
  availableBalance: Decimal,
  referencePrice: Decimal,
  constraints: PairConstraints
): Decimal {
  const targetLeverage = new Decimal(config.targetLeverage ?? config.leverage ?? 1);
  const allocationPercent = new Decimal(config.capitalAllocationPercent ?? 90);

  const totalNotional = availableBalance
    .mul(targetLeverage)
    .mul(allocationPercent.div(100));

  const totalLevels = config.levels;
  let quantity = totalNotional.div(totalLevels).div(referencePrice);

  const truncationFactor = new Decimal(10).pow(constraints.baseDecimalPlaces);
  quantity = quantity.mul(truncationFactor).floor().div(truncationFactor);

  const minQty = new Decimal(constraints.minBaseAmount);
  if (quantity.lessThan(minQty)) {
    quantity = minQty;
  }

  return quantity;
}

/**
 * Generate all valid grid levels inside [lowerBound, upperBound].
 * Levels are sorted by distance from range center (innermost first).
 */
export function generateGridLevels(
  config: BotConfig,
  lowerBound: Decimal,
  upperBound: Decimal,
  referencePrice: Decimal,
  constraints: PairConstraints,
  quantity: Decimal,
  seed: string
): GridLevel[] {
  const spacing = computeSpacing(lowerBound, upperBound, config.levels);
  const rangeCenter = lowerBound.plus(upperBound).div(2);
  const levels: GridLevel[] = [];

  // Generate levels from lowerBound to upperBound
  for (let i = 0; i < config.levels; i++) {
    const price = lowerBound.plus(spacing.mul(i));
    const priceStr = priceToString(price, constraints.tickSize);
    
    // Determine side based on reference price
    const side: 'BUY' | 'SELL' = price.lt(referencePrice) ? 'BUY' : 'SELL';
    
    // Skip if price equals reference (no order at exact reference)
    if (price.eq(referencePrice)) {
      continue;
    }

    const distanceFromRef = price.minus(referencePrice).abs();
    const distanceFromCenter = price.minus(rangeCenter).abs();

    levels.push({
      levelIndex: i + 1,
      price,
      priceStr,
      side,
      customerOrderId: buildCOID(side, i + 1, seed),
      state: 'missing',
      quantity,
      quantityStr: qtyToString(quantity, constraints.baseDecimalPlaces),
      distanceFromRef,
      distanceFromCenter,
    });
  }

  // Sort by distance from reference price (nearest first)
  // Tie-break: closer to range center first, then lower level index
  levels.sort((a, b) => {
    const distCmp = a.distanceFromRef.cmp(b.distanceFromRef);
    if (distCmp !== 0) return distCmp;
    
    const centerCmp = a.distanceFromCenter.cmp(b.distanceFromCenter);
    if (centerCmp !== 0) return centerCmp;
    
    return a.levelIndex - b.levelIndex;
  });

  return levels;
}

/**
 * Select exactly N levels for active placement.
 * 
 * Selection rules:
 * - Only levels inside [lowerBound, upperBound]
 * - Bids: price < currentPrice
 * - Asks: price > currentPrice
 * - Select nearest to currentPrice until N total
 * 
 * This naturally produces:
 * - More bids when price is near upperBound
 * - More asks when price is near lowerBound
 * - Balanced split when price is near center
 */
export function selectActiveLevels(
  allLevels: GridLevel[],
  currentPrice: Decimal,
  n: number
): GridLevel[] {
  const candidates: GridLevel[] = [];

  for (const level of allLevels) {
    // Determine if this level should be a bid or ask based on current price
    const shouldBeBid = level.price.lt(currentPrice);
    const shouldBeAsk = level.price.gt(currentPrice);
    
    if (shouldBeBid || shouldBeAsk) {
      candidates.push({
        ...level,
        side: shouldBeBid ? 'BUY' : 'SELL',
      });
    }
  }

  // Sort by distance from current price (nearest first)
  candidates.sort((a, b) => {
    const distA = a.price.minus(currentPrice).abs();
    const distB = b.price.minus(currentPrice).abs();
    return distA.cmp(distB);
  });

  // Select exactly N levels (or all candidates if fewer than N)
  const selected = candidates.slice(0, n);

  return selected;
}

/**
 * Initialize grid state.
 */
export function initGridState(
  config: BotConfig,
  lowerBound: Decimal,
  upperBound: Decimal,
  referencePrice: Decimal,
  currentPrice: Decimal,
  constraints: PairConstraints,
  availableBalance: Decimal,
  seed: string
): GridState {
  const quantity = config.dynamicSizing
    ? calculateQuantityPerLevel(config, availableBalance, referencePrice, constraints)
    : new Decimal(config.quantityPerLevel);

  const rangeCenter = lowerBound.plus(upperBound).div(2);
  const allLevels = generateGridLevels(config, lowerBound, upperBound, referencePrice, constraints, quantity, seed);
  const activeLevels = selectActiveLevels(allLevels, currentPrice, config.levels);

  const activeBids = activeLevels.filter(l => l.side === 'BUY').length;
  const activeAsks = activeLevels.filter(l => l.side === 'SELL').length;

  return {
    levels: allLevels,
    lowerBound,
    upperBound,
    rangeCenter,
    referencePrice,
    currentPrice,
    activeLevels,
    activeBidCount: activeBids,
    activeAskCount: activeAsks,
  };
}

/**
 * Update grid state when price changes.
 * Recomputes active levels and returns what changed.
 */
export function updateGridState(
  grid: GridState,
  config: BotConfig,
  currentPrice: Decimal
): {
  toPlace: GridLevel[];
  toCancel: GridLevel[];
  bidCount: number;
  askCount: number;
} {
  grid.currentPrice = currentPrice;

  // Recompute active levels based on new price
  const newActiveLevels = selectActiveLevels(grid.levels, currentPrice, config.levels);
  const newActiveIds = new Set(newActiveLevels.map(l => l.customerOrderId));
  const oldActiveIds = new Set(grid.activeLevels.map(l => l.customerOrderId));

  // Find levels to place (in new set but not yet placed)
  const toPlace: GridLevel[] = [];
  for (const level of newActiveLevels) {
    const wasActive = oldActiveIds.has(level.customerOrderId);
    if (!wasActive || level.state === 'missing' || level.state === 'cancelled') {
      toPlace.push(level);
    }
  }

  // Find levels to cancel (in old set but not in new set, and still active)
  const toCancel: GridLevel[] = [];
  for (const level of grid.activeLevels) {
    const shouldBeActive = newActiveIds.has(level.customerOrderId);
    if (!shouldBeActive && (level.state === 'active' || level.state === 'pending')) {
      toCancel.push(level);
    }
  }

  // Update active levels
  grid.activeLevels = newActiveLevels;
  grid.activeBidCount = newActiveLevels.filter(l => l.side === 'BUY').length;
  grid.activeAskCount = newActiveLevels.filter(l => l.side === 'SELL').length;

  return {
    toPlace,
    toCancel,
    bidCount: grid.activeBidCount,
    askCount: grid.activeAskCount,
  };
}

/**
 * Mark a level as filled.
 */
export function markLevelFilled(
  grid: GridState,
  customerOrderId: string | undefined,
  exchangeOrderId: string
): GridLevel | null {
  for (const level of grid.levels) {
    if (level.exchangeOrderId === exchangeOrderId || level.customerOrderId === customerOrderId) {
      level.state = 'filled';
      level.filledAt = new Date().toISOString();
      log.info(
        { level: level.levelIndex, side: level.side, price: level.priceStr },
        'Grid level filled'
      );
      return level;
    }
  }
  return null;
}

/**
 * Mark a level as active (order placed and confirmed).
 */
export function markLevelActive(
  grid: GridState,
  customerOrderId: string,
  exchangeOrderId: string
): boolean {
  for (const level of grid.levels) {
    if (level.customerOrderId === customerOrderId) {
      level.state = 'active';
      level.exchangeOrderId = exchangeOrderId;
      return true;
    }
  }
  return false;
}

/**
 * Mark a level as cancelled.
 */
export function markLevelCancelled(
  grid: GridState,
  customerOrderId: string
): boolean {
  for (const level of grid.levels) {
    if (level.customerOrderId === customerOrderId) {
      level.state = 'cancelled';
      level.exchangeOrderId = undefined;
      return true;
    }
  }
  return false;
}

/**
 * Get levels that need placement (in active set, state is missing/cancelled, no exchangeOrderId).
 */
export function getLevelsToPlace(grid: GridState): GridLevel[] {
  return grid.activeLevels.filter(
    l => (l.state === 'missing' || l.state === 'cancelled') && !l.exchangeOrderId
  );
}

/**
 * Get levels that should be cancelled (not in active set but have exchangeOrderId).
 */
export function getLevelsToCancel(grid: GridState): GridLevel[] {
  const activeIds = new Set(grid.activeLevels.map(l => l.customerOrderId));
  return grid.levels.filter(
    l => !activeIds.has(l.customerOrderId) && l.exchangeOrderId && l.state !== 'filled'
  );
}

/**
 * Log grid state for observability.
 */
export function logGridState(grid: GridState, config: BotConfig): void {
  const pricePosition = grid.currentPrice
    .minus(grid.lowerBound)
    .div(grid.upperBound.minus(grid.lowerBound))
    .mul(100)
    .toFixed(1);

  log.info(
    {
      referencePrice: grid.referencePrice.toString(),
      currentPrice: grid.currentPrice.toString(),
      lowerBound: grid.lowerBound.toString(),
      upperBound: grid.upperBound.toString(),
      rangeCenter: grid.rangeCenter.toString(),
      pricePosition: pricePosition + '%',
      activeBids: grid.activeBidCount,
      activeAsks: grid.activeAskCount,
      totalActive: grid.activeBidCount + grid.activeAskCount,
      targetLevels: config.levels,
      skew: grid.activeBidCount > grid.activeAskCount ? 'bullish (more bids)' : 
            grid.activeAskCount > grid.activeBidCount ? 'bearish (more asks)' : 'balanced',
    },
    'Grid state summary'
  );

  // Log why we have this bid/ask split
  if (grid.activeBidCount !== grid.activeAskCount) {
    const explanation = grid.activeBidCount > grid.activeAskCount
      ? 'Price is in upper portion of range → more bids, fewer asks'
      : 'Price is in lower portion of range → more asks, fewer bids';
    log.info({ explanation }, 'Bid/ask split rationale');
  }
}
