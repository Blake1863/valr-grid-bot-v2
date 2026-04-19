/**
 * Grid Builder — OKX/Bybit Style
 * 
 * Implements arithmetic and geometric grid construction matching exchange behavior.
 * 
 * Key principles:
 * - gridCount = number of INTERVALS (not levels)
 * - Arithmetic: equal price difference between adjacent levels
 * - Geometric: equal ratio/percentage between adjacent levels
 * - All levels are unique, sorted, and respect tick/qty precision
 */

import { Decimal } from 'decimal.js';
import type { BotConfig } from '../config/schema.js';
import type { PairConstraints } from '../exchange/pairMetadata.js';
import { priceToString, qtyToString, roundToTick } from '../exchange/pairMetadata.js';

export interface GridLevel {
  levelIndex: number;          // 0 to gridCount (boundaries) or 1 to gridCount-1 (orders)
  price: Decimal;
  priceStr: string;
  side?: 'BUY' | 'SELL';       // Determined by reference price in neutral mode
  quantity?: Decimal;
  quantityStr?: string;
  customerOrderId?: string;
  exchangeOrderId?: string;
  state: 'missing' | 'pending' | 'active' | 'filled' | 'cancelled';
  role?: 'entry' | 'exit';     // entry = initial grid order, exit = completion order
  adjacentTargetIndex?: number; // Level index to place after fill
}

export interface GridConstruction {
  levels: GridLevel[];         // All grid levels (boundaries)
  lowerBound: Decimal;
  upperBound: Decimal;
  gridCount: number;
  gridMode: 'arithmetic' | 'geometric';
  spacing?: Decimal;           // Arithmetic spacing (price difference)
  ratio?: Decimal;             // Geometric ratio (multiplier)
}

/**
 * Build customer order ID — max 50 chars, VALR-compliant.
 */
function buildCOID(mode: string, side: 'BUY' | 'SELL', level: number, seed: string): string {
  const m = mode === 'neutral' ? 'N' : mode === 'long' ? 'L' : 'S';
  const s = side === 'BUY' ? 'B' : 'S';
  return `grid-${m}-${s}${level}-${seed}`.slice(0, 50);
}

/**
 * Generate arithmetic grid levels.
 * 
 * Formula: level[i] = lowerBound + (i * spacing)
 * where spacing = (upperBound - lowerBound) / gridCount
 * 
 * Example: lower=100, upper=400, gridCount=3
 *   spacing = (400-100)/3 = 100
 *   levels: 100, 200, 300, 400 (4 boundaries, 3 intervals)
 */
export function buildArithmeticGrid(
  lowerBound: Decimal,
  upperBound: Decimal,
  gridCount: number,
  constraints: PairConstraints
): GridLevel[] {
  const spacing = upperBound.minus(lowerBound).div(gridCount);
  const levels: GridLevel[] = [];

  for (let i = 0; i <= gridCount; i++) {
    const price = lowerBound.plus(spacing.mul(i));
    const roundedPrice = roundToTick(price, constraints.tickSize);
    const priceStr = priceToString(roundedPrice, constraints.tickSize);

    levels.push({
      levelIndex: i,
      price: roundedPrice,
      priceStr,
      state: 'missing',
    });
  }

  return levels;
}

/**
 * Generate geometric grid levels.
 * 
 * Formula: level[i] = lowerBound * (ratio ^ i)
 * where ratio = (upperBound / lowerBound) ^ (1 / gridCount)
 * 
 * Example: lower=100, upper=400, gridCount=3
 *   ratio = (400/100)^(1/3) = 4^0.333 = 1.5874...
 *   levels: 100, 158.74, 251.98, 400
 */
export function buildGeometricGrid(
  lowerBound: Decimal,
  upperBound: Decimal,
  gridCount: number,
  constraints: PairConstraints
): GridLevel[] {
  const ratio = upperBound.div(lowerBound).pow(1 / gridCount);
  const levels: GridLevel[] = [];

  for (let i = 0; i <= gridCount; i++) {
    const price = lowerBound.mul(ratio.pow(i));
    const roundedPrice = roundToTick(price, constraints.tickSize);
    const priceStr = priceToString(roundedPrice, constraints.tickSize);

    levels.push({
      levelIndex: i,
      price: roundedPrice,
      priceStr,
      state: 'missing',
    });
  }

  // Ensure upper bound is exact
  const lastIdx = levels.length - 1;
  const upperRounded = roundToTick(upperBound, constraints.tickSize);
  levels[lastIdx].price = upperRounded;
  levels[lastIdx].priceStr = priceToString(upperRounded, constraints.tickSize);

  return levels;
}

/**
 * Build complete grid construction.
 */
export function buildGrid(
  config: BotConfig,
  constraints: PairConstraints
): GridConstruction {
  const lowerBound = new Decimal(config.lowerBound);
  const upperBound = new Decimal(config.upperBound);
  const gridCount = config.gridCount;

  let levels: GridLevel[];

  if (config.gridMode === 'arithmetic') {
    levels = buildArithmeticGrid(lowerBound, upperBound, gridCount, constraints);
  } else {
    levels = buildGeometricGrid(lowerBound, upperBound, gridCount, constraints);
  }

  // Remove duplicate prices (can happen after rounding)
  const seen = new Set<string>();
  levels = levels.filter(l => {
    if (seen.has(l.priceStr)) return false;
    seen.add(l.priceStr);
    return true;
  });

  // Ensure levels are sorted by price
  levels.sort((a, b) => a.price.cmp(b.price));

  // Re-index after dedup
  levels.forEach((l, i) => { l.levelIndex = i; });

  return {
    levels,
    lowerBound,
    upperBound,
    gridCount,
    gridMode: config.gridMode,
  };
}

/**
 * Assign sides to grid levels based on reference price (neutral mode).
 * 
 * OKX/Bybit behavior:
 * - Levels BELOW reference price → BUY orders
 * - Levels ABOVE reference price → SELL orders
 * - Level AT reference price → skipped (no order)
 */
export function assignNeutralSides(
  levels: GridLevel[],
  referencePrice: Decimal,
  seed: string
): GridLevel[] {
  return levels.map((level, idx) => {
    // Skip boundary levels (0 and last) for initial placement
    // They serve as range limits, not order levels
    if (idx === 0 || idx === levels.length - 1) {
      return level;
    }

    const price = level.price;
    
    // Determine side based on reference price
    if (price.lt(referencePrice)) {
      return {
        ...level,
        side: 'BUY',
        customerOrderId: buildCOID('neutral', 'BUY', idx, seed),
        adjacentTargetIndex: idx + 1, // After buy fills, sell at next level up
      };
    } else if (price.gt(referencePrice)) {
      return {
        ...level,
        side: 'SELL',
        customerOrderId: buildCOID('neutral', 'SELL', idx, seed),
        adjacentTargetIndex: idx - 1, // After sell fills, buy at next level down
      };
    } else {
      // Price exactly at reference — skip
      return level;
    }
  });
}

/**
 * Calculate quantity per level from available balance.
 * 
 * Formula:
 *   totalNotional = balance * leverage * (allocation% / 100)
 *   quantityPerLevel = totalNotional / gridCount / referencePrice
 */
export function calculateQuantityPerLevel(
  config: BotConfig,
  availableBalance: Decimal,
  referencePrice: Decimal,
  constraints: PairConstraints
): Decimal {
  const targetLeverage = new Decimal(config.leverage);
  const allocationPercent = new Decimal(config.capitalAllocationPercent);

  const totalNotional = availableBalance
    .mul(targetLeverage)
    .mul(allocationPercent.div(100));

  // Divide across grid intervals
  let quantity = totalNotional.div(config.gridCount).div(referencePrice);

  // Apply quantity precision
  const truncationFactor = new Decimal(10).pow(constraints.baseDecimalPlaces);
  quantity = quantity.mul(truncationFactor).floor().div(truncationFactor);

  // Enforce minimum
  const minQty = new Decimal(constraints.minBaseAmount);
  if (quantity.lessThan(minQty)) {
    quantity = minQty;
  }

  return quantity;
}

/**
 * Get the adjacent target level for cycle completion.
 * 
 * OKX/Bybit neutral mode:
 * - Buy at level[i] fills → place sell at level[i+1]
 * - Sell at level[i] fills → place buy at level[i-1]
 */
export function getAdjacentTarget(
  filledLevelIndex: number,
  filledSide: 'BUY' | 'SELL',
  levels: GridLevel[]
): number | null {
  if (filledSide === 'BUY') {
    // Buy filled → place sell at next higher level
    const targetIdx = filledLevelIndex + 1;
    return targetIdx < levels.length ? targetIdx : null;
  } else {
    // Sell filled → place buy at next lower level
    const targetIdx = filledLevelIndex - 1;
    return targetIdx >= 0 ? targetIdx : null;
  }
}
