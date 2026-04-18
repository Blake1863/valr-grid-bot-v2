/**
 * Grid Pair Manager — Explicit pair tracking for symmetric grid.
 * 
 * Core concepts:
 * - Grid is N pairs (bid + ask), not loose orders
 * - Each pair has a levelIndex (1 to N)
 * - Pairs can be: active, partial (one leg filled), complete (both filled), or missing
 * - Replenishment replaces completed pairs at their original level
 * - Grid always stays within configured level range (1 to N)
 * 
 * This prevents:
 * - Grid decay into one-sided state
 * - Unlimited level expansion from repeated fills
 * - Directional bias from asymmetric replenishment
 */

import Decimal from 'decimal.js';
import type { BotConfig } from '../config/schema.js';
import type { PairConstraints } from '../exchange/pairMetadata.js';
import { priceToString, qtyToString } from '../exchange/pairMetadata.js';
import { createLogger } from '../app/logger.js';

const log = createLogger('gridPairManager');

export type PairLegState = 'missing' | 'active' | 'filled';
export type PairState = 'active' | 'partial' | 'complete' | 'missing';

export interface GridPairLeg {
  level: number;
  side: 'BUY' | 'SELL';
  price: Decimal;
  priceStr: string;
  quantity: Decimal;
  quantityStr: string;
  customerOrderId: string;
  exchangeOrderId?: string;
  state: PairLegState;
  filledAt?: string;
  updatedAt?: string;
}

export interface GridPair {
  pairId: string;           // e.g., "pair-1", "pair-2"
  levelIndex: number;        // 1 to N (configured levels)
  bidLeg: GridPairLeg;
  askLeg: GridPairLeg;
  state: PairState;
  createdAt: string;
  updatedAt: string;
  completedAt?: string;      // When both legs filled
}

export interface GridState {
  pairs: GridPair[];
  referencePrice: Decimal;
  totalActiveBids: number;
  totalActiveAsks: number;
  partialPairs: number;
  completePairs: number;
  missingPairs: number;
}

/** Build customerOrderId for a pair leg — max 50 chars */
function buildCOID(side: 'BUY' | 'SELL', level: number, seed: string): string {
  const s = side === 'BUY' ? 'B' : 'S';
  return `grid-${s}-${level}-${seed}`.slice(0, 50);
}

/** Price one step below reference (for BUY at level i) */
function buyLevelPrice(ref: Decimal, spacing: Decimal, spacingMode: 'percent' | 'absolute', i: number): Decimal {
  if (spacingMode === 'percent') {
    const factor = new Decimal(1).minus(spacing.div(100));
    return ref.mul(factor.pow(i));
  }
  return ref.minus(spacing.mul(i));
}

/** Price one step above reference (for SELL at level i) */
function sellLevelPrice(ref: Decimal, spacing: Decimal, spacingMode: 'percent' | 'absolute', i: number): Decimal {
  if (spacingMode === 'percent') {
    const factor = new Decimal(1).plus(spacing.div(100));
    return ref.mul(factor.pow(i));
  }
  return ref.plus(spacing.mul(i));
}

/**
 * Calculate dynamic quantity per level based on available balance.
 * For symmetric grid: totalNotional / (levels * 2)
 */
export function calculateQuantityPerLevel(
  config: BotConfig,
  availableBalance: Decimal,
  referencePrice: Decimal,
  constraints: PairConstraints
): Decimal {
  const targetLeverage = new Decimal(config.targetLeverage ?? config.leverage ?? 1);
  const allocationPercent = new Decimal(config.capitalAllocationPercent ?? 90);

  // Total notional we want to deploy
  const totalNotional = availableBalance
    .mul(targetLeverage)
    .mul(allocationPercent.div(100));

  // For neutral: levels * 2 (both sides)
  const totalLevels = config.levels * 2;

  // Notional per level
  let quantity = totalNotional.div(totalLevels).div(referencePrice);

  // Round down to baseDecimalPlaces
  const truncationFactor = new Decimal(10).pow(constraints.baseDecimalPlaces);
  quantity = quantity.mul(truncationFactor).floor().div(truncationFactor);

  // Ensure minimum order size
  const minQty = new Decimal(constraints.minBaseAmount);
  if (quantity.lessThan(minQty)) {
    quantity = minQty;
  }

  return quantity;
}

/**
 * Create a single grid pair at the given level index.
 */
export function createGridPair(
  config: BotConfig,
  referencePrice: Decimal,
  constraints: PairConstraints,
  levelIndex: number,
  quantity: Decimal,
  seed: string
): GridPair {
  const spacing = new Decimal(config.spacingValue);
  const now = new Date().toISOString();

  const bidPrice = buyLevelPrice(referencePrice, spacing, config.spacingMode, levelIndex);
  const askPrice = sellLevelPrice(referencePrice, spacing, config.spacingMode, levelIndex);

  const bidLeg: GridPairLeg = {
    level: levelIndex,
    side: 'BUY',
    price: bidPrice,
    priceStr: priceToString(bidPrice, constraints.tickSize),
    quantity,
    quantityStr: qtyToString(quantity, constraints.baseDecimalPlaces),
    customerOrderId: buildCOID('BUY', levelIndex, seed),
    state: 'missing',
  };

  const askLeg: GridPairLeg = {
    level: levelIndex,
    side: 'SELL',
    price: askPrice,
    priceStr: priceToString(askPrice, constraints.tickSize),
    quantity,
    quantityStr: qtyToString(quantity, constraints.baseDecimalPlaces),
    customerOrderId: buildCOID('SELL', levelIndex, seed),
    state: 'missing',
  };

  return {
    pairId: `pair-${levelIndex}`,
    levelIndex,
    bidLeg,
    askLeg,
    state: 'missing',
    createdAt: now,
    updatedAt: now,
  };
}

/**
 * Build the complete grid state with all pairs.
 */
export function buildGridState(
  config: BotConfig,
  referencePrice: Decimal,
  constraints: PairConstraints,
  availableBalance: Decimal,
  seed: string
): GridState {
  const quantity = config.dynamicSizing
    ? calculateQuantityPerLevel(config, availableBalance, referencePrice, constraints)
    : new Decimal(config.quantityPerLevel);

  const pairs: GridPair[] = [];
  for (let i = 1; i <= config.levels; i++) {
    pairs.push(createGridPair(config, referencePrice, constraints, i, quantity, seed));
  }

  return {
    pairs,
    referencePrice,
    totalActiveBids: 0,
    totalActiveAsks: 0,
    partialPairs: 0,
    completePairs: 0,
    missingPairs: config.levels,
  };
}

/**
 * Update pair state based on leg states.
 */
function updatePairState(pair: GridPair): void {
  const bidState = pair.bidLeg.state;
  const askState = pair.askLeg.state;

  if (bidState === 'filled' && askState === 'filled') {
    pair.state = 'complete';
    pair.completedAt = new Date().toISOString();
  } else if (bidState === 'filled' || askState === 'filled') {
    pair.state = 'partial';
  } else if (bidState === 'active' || askState === 'active') {
    pair.state = 'active';
  } else {
    pair.state = 'missing';
  }

  pair.updatedAt = new Date().toISOString();
}

/**
 * Mark a leg as active (order placed).
 */
export function markLegActive(
  grid: GridState,
  customerOrderId: string,
  exchangeOrderId: string
): boolean {
  for (const pair of grid.pairs) {
    if (pair.bidLeg.customerOrderId === customerOrderId) {
      pair.bidLeg.state = 'active';
      pair.bidLeg.exchangeOrderId = exchangeOrderId;
      updatePairState(pair);
      return true;
    }
    if (pair.askLeg.customerOrderId === customerOrderId) {
      pair.askLeg.state = 'active';
      pair.askLeg.exchangeOrderId = exchangeOrderId;
      updatePairState(pair);
      return true;
    }
  }
  return false;
}

/**
 * Mark a leg as filled.
 * Returns the pair that was filled for replenishment logic.
 */
export function markLegFilled(
  grid: GridState,
  customerOrderId: string | undefined,
  exchangeOrderId: string
): GridPair | null {
  for (const pair of grid.pairs) {
    const isBid = pair.bidLeg.exchangeOrderId === exchangeOrderId || 
                  pair.bidLeg.customerOrderId === customerOrderId;
    const isAsk = pair.askLeg.exchangeOrderId === exchangeOrderId ||
                  pair.askLeg.customerOrderId === customerOrderId;

    if (isBid) {
      pair.bidLeg.state = 'filled';
      pair.bidLeg.filledAt = new Date().toISOString();
      updatePairState(pair);
      log.info(
        { pairId: pair.pairId, level: pair.levelIndex, side: 'BUY', newState: pair.state },
        'Bid leg filled'
      );
      return pair;
    }
    if (isAsk) {
      pair.askLeg.state = 'filled';
      pair.askLeg.filledAt = new Date().toISOString();
      updatePairState(pair);
      log.info(
        { pairId: pair.pairId, level: pair.levelIndex, side: 'SELL', newState: pair.state },
        'Ask leg filled'
      );
      return pair;
    }
  }
  return null;
}

/**
 * Mark a leg as cancelled/missing.
 */
export function markLegMissing(
  grid: GridState,
  customerOrderId: string
): boolean {
  for (const pair of grid.pairs) {
    if (pair.bidLeg.customerOrderId === customerOrderId) {
      pair.bidLeg.state = 'missing';
      pair.bidLeg.exchangeOrderId = undefined;
      updatePairState(pair);
      return true;
    }
    if (pair.askLeg.customerOrderId === customerOrderId) {
      pair.askLeg.state = 'missing';
      pair.askLeg.exchangeOrderId = undefined;
      updatePairState(pair);
      return true;
    }
  }
  return false;
}

/**
 * Get all legs that need to be placed (state = 'missing').
 * Returns GridPairLeg[] with full order data.
 */
export function getMissingLegs(grid: GridState): GridPairLeg[] {
  const missing: GridPairLeg[] = [];
  for (const pair of grid.pairs) {
    if (pair.bidLeg.state === 'missing') {
      missing.push(pair.bidLeg);
    }
    if (pair.askLeg.state === 'missing') {
      missing.push(pair.askLeg);
    }
  }
  return missing;
}

/**
 * Get all active legs (state = 'active').
 */
export function getActiveLegs(grid: GridState): GridPairLeg[] {
  const active: GridPairLeg[] = [];
  for (const pair of grid.pairs) {
    if (pair.bidLeg.state === 'active') {
      active.push(pair.bidLeg);
    }
    if (pair.askLeg.state === 'active') {
      active.push(pair.askLeg);
    }
  }
  return active;
}

/**
 * Get completed pairs that need replacement.
 */
export function getCompletedPairs(grid: GridState): GridPair[] {
  return grid.pairs.filter(p => p.state === 'complete');
}

/**
 * Get partial pairs (one leg filled, one missing/active).
 */
export function getPartialPairs(grid: GridState): GridPair[] {
  return grid.pairs.filter(p => p.state === 'partial');
}

/**
 * Recalculate grid statistics.
 */
export function recalculateGridStats(grid: GridState): void {
  let activeBids = 0;
  let activeAsks = 0;
  let partial = 0;
  let complete = 0;
  let missing = 0;

  for (const pair of grid.pairs) {
    updatePairState(pair);
    
    if (pair.bidLeg.state === 'active') activeBids++;
    if (pair.askLeg.state === 'active') activeAsks++;
    
    switch (pair.state) {
      case 'partial': partial++; break;
      case 'complete': complete++; break;
      case 'missing': missing++; break;
    }
  }

  grid.totalActiveBids = activeBids;
  grid.totalActiveAsks = activeAsks;
  grid.partialPairs = partial;
  grid.completePairs = complete;
  grid.missingPairs = missing;
}

/**
 * Check if grid needs rebuilding due to price drift.
 * Returns true if reference price has moved beyond the outer grid levels.
 */
export function needsRecenter(
  grid: GridState,
  newReferencePrice: Decimal,
  config: BotConfig,
  constraints: PairConstraints
): boolean {
  if (grid.pairs.length === 0) return true;

  const spacing = new Decimal(config.spacingValue);
  const outerBidLevel = config.levels;
  const outerAskLevel = config.levels;

  // Calculate current outer levels based on OLD reference
  const oldOuterBid = grid.pairs[0].bidLeg.price;
  const oldOuterAsk = grid.pairs[0].askLeg.price;

  // Calculate what outer levels WOULD BE with new reference
  const newOuterBid = buyLevelPrice(newReferencePrice, spacing, config.spacingMode, outerBidLevel);
  const newOuterAsk = sellLevelPrice(newReferencePrice, spacing, config.spacingMode, outerAskLevel);

  // If new reference would put current price outside grid bounds, recenter
  // Threshold: if price moved more than 50% of grid range
  const gridRange = oldOuterAsk.minus(oldOuterBid);
  const priceMove = newReferencePrice.minus(grid.referencePrice).abs();
  const threshold = gridRange.mul(0.5);

  const shouldRecenter = priceMove.gt(threshold);
  
  if (shouldRecenter) {
    log.info(
      {
        oldRef: grid.referencePrice.toString(),
        newRef: newReferencePrice.toString(),
        priceMove: priceMove.toString(),
        threshold: threshold.toString(),
        gridRange: gridRange.toString(),
      },
      'Grid recenter triggered — price moved beyond threshold'
    );
  }

  return shouldRecenter;
}

/**
 * Rebuild grid state with new reference price.
 * Preserves filled legs (exposure), replaces missing/active legs.
 */
export function rebuildGridWithNewReference(
  grid: GridState,
  config: BotConfig,
  newReferencePrice: Decimal,
  constraints: PairConstraints,
  availableBalance: Decimal,
  seed: string
): GridState {
  const quantity = config.dynamicSizing
    ? calculateQuantityPerLevel(config, availableBalance, newReferencePrice, constraints)
    : new Decimal(config.quantityPerLevel);

  const spacing = new Decimal(config.spacingValue);
  const now = new Date().toISOString();

  const newPairs: GridPair[] = [];

  for (let i = 1; i <= config.levels; i++) {
    const oldPair = grid.pairs.find(p => p.levelIndex === i);
    
    // Create new pair with new reference price
    const newPair = createGridPair(config, newReferencePrice, constraints, i, quantity, seed);

    // Preserve filled legs (we still have this exposure)
    if (oldPair) {
      if (oldPair.bidLeg.state === 'filled') {
        newPair.bidLeg = { ...oldPair.bidLeg };
      }
      if (oldPair.askLeg.state === 'filled') {
        newPair.askLeg = { ...oldPair.askLeg };
      }
    }

    // Update prices for non-filled legs
    if (newPair.bidLeg.state === 'missing') {
      const newBidPrice = buyLevelPrice(newReferencePrice, spacing, config.spacingMode, i);
      newPair.bidLeg.price = newBidPrice;
      newPair.bidLeg.priceStr = priceToString(newBidPrice, constraints.tickSize);
    }
    if (newPair.askLeg.state === 'missing') {
      const newAskPrice = sellLevelPrice(newReferencePrice, spacing, config.spacingMode, i);
      newPair.askLeg.price = newAskPrice;
      newPair.askLeg.priceStr = priceToString(newAskPrice, constraints.tickSize);
    }

    newPair.updatedAt = now;
    newPairs.push(newPair);
  }

  const newGrid: GridState = {
    pairs: newPairs,
    referencePrice: newReferencePrice,
    totalActiveBids: 0,
    totalActiveAsks: 0,
    partialPairs: 0,
    completePairs: 0,
    missingPairs: 0,
  };

  recalculateGridStats(newGrid);

  log.info(
    {
      newRef: newReferencePrice.toString(),
      activeBids: newGrid.totalActiveBids,
      activeAsks: newGrid.totalActiveAsks,
      partialPairs: newGrid.partialPairs,
      completePairs: newGrid.completePairs,
      missingPairs: newGrid.missingPairs,
    },
    'Grid rebuilt with new reference price'
  );

  return newGrid;
}

/**
 * Replace a completed pair — reset both legs to missing so they get placed.
 * This is the core "keep grid full" logic.
 */
export function replaceCompletedPair(
  grid: GridState,
  pairId: string,
  config: BotConfig,
  referencePrice: Decimal,
  constraints: PairConstraints,
  quantity: Decimal,
  seed: string
): GridPair | null {
  const pairIndex = grid.pairs.findIndex(p => p.pairId === pairId);
  if (pairIndex === -1) return null;

  const oldPair = grid.pairs[pairIndex];
  const now = new Date().toISOString();

  // Create fresh pair at same level
  const newPair = createGridPair(config, referencePrice, constraints, oldPair.levelIndex, quantity, seed);
  newPair.createdAt = oldPair.createdAt;  // Preserve original creation time
  newPair.updatedAt = now;

  grid.pairs[pairIndex] = newPair;
  recalculateGridStats(grid);

  log.info(
    { pairId, level: oldPair.levelIndex, completedAt: oldPair.completedAt },
    'Completed pair replaced — grid restored to full'
  );

  return newPair;
}

/**
 * Log current grid state for observability.
 */
export function logGridState(grid: GridState, config: BotConfig): void {
  log.info(
    {
      referencePrice: grid.referencePrice.toString(),
      totalActiveBids: grid.totalActiveBids,
      totalActiveAsks: grid.totalActiveAsks,
      partialPairs: grid.partialPairs,
      completePairs: grid.completePairs,
      missingPairs: grid.missingPairs,
      targetLevels: config.levels,
      isFull: grid.totalActiveBids === config.levels && grid.totalActiveAsks === config.levels,
    },
    'Grid state summary'
  );

  for (const pair of grid.pairs) {
    log.debug(
      {
        pairId: pair.pairId,
        level: pair.levelIndex,
        state: pair.state,
        bid: { price: pair.bidLeg.priceStr, state: pair.bidLeg.state },
        ask: { price: pair.askLeg.priceStr, state: pair.askLeg.state },
      },
      `Pair ${pair.levelIndex}`
    );
  }
}
