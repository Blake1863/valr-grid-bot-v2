/**
 * Grid level generator — LEGACY support for directional modes.
 * 
 * For neutral symmetric grid with explicit pair tracking, use gridPairManager.ts.
 * 
 * Pure functions — no I/O, testable in isolation.
 *
 * long_only:  laddered BUY orders below reference price
 * short_only: laddered SELL orders above reference price
 *
 * percent spacing example (3 levels, 0.4%, ref=100):
 *   BUY  level 1: 100 * 0.996   = 99.60
 *   BUY  level 2: 100 * 0.996²  = 99.20
 *   BUY  level 3: 100 * 0.996³  = 98.81
 *   SELL level 1: 100 * 1.004   = 100.40
 *   SELL level 2: 100 * 1.004²  = 100.80
 *   SELL level 3: 100 * 1.004³  = 101.21
 */

import Decimal from 'decimal.js';
import { priceToString, qtyToString } from '../exchange/pairMetadata.js';
import type { PairConstraints } from '../exchange/pairMetadata.js';
import type { BotConfig } from '../config/schema.js';

/**
 * Calculate dynamic quantity per level based on available balance.
 *
 * Formula:
 *   totalNotional = availableBalance × (targetLeverage / 100) × (capitalAllocationPercent / 100)
 *   notionalPerLevel = totalNotional / totalLevels
 *   quantityPerLevel = notionalPerLevel / referencePrice
 *
 * For neutral mode: totalLevels = levels × 2 (both sides)
 * For long_only/short_only: totalLevels = levels (one side)
 */
export function calculateDynamicQuantity(
  config: BotConfig,
  availableBalance: Decimal,
  referencePrice: Decimal,
  constraints: PairConstraints
): Decimal {
  const targetLeverage = new Decimal(config.targetLeverage ?? config.leverage ?? 1);
  const allocationPercent = new Decimal(config.capitalAllocationPercent ?? 90);

  // Calculate total notional we want to deploy
  const totalNotional = availableBalance
    .mul(targetLeverage)
    .mul(allocationPercent.div(100));

  // Total grid levels (both sides for neutral, one side for directional)
  const totalLevels = config.mode === 'neutral'
    ? config.levels * 2
    : config.levels;

  // Notional per level
  const notionalPerLevel = totalNotional.div(totalLevels);

  // Quantity per level in base currency
  let quantity = notionalPerLevel.div(referencePrice);

  // Round down to baseDecimalPlaces (conservative — never over-allocate)
  const baseDecimals = constraints.baseDecimalPlaces;
  const truncationFactor = new Decimal(10).pow(baseDecimals);
  quantity = quantity.mul(truncationFactor).floor().div(truncationFactor);

  // Ensure minimum order size
  const minQty = new Decimal(constraints.minBaseAmount);
  if (quantity.lessThan(minQty)) {
    quantity = minQty;
  }

  return quantity;
}

export interface GridLevel {
  level: number;       // 1-indexed distance from reference
  side: 'BUY' | 'SELL';
  price: Decimal;
  priceStr: string;    // rounded to tickSize
  quantity: Decimal;
  quantityStr: string; // truncated to baseDecimalPlaces
  customerOrderId: string;
  pairId?: string;     // Grid pair ID for pair-based tracking
}

/** Build customerOrderId — max 50 chars, alphanumeric + dashes */
function buildCOID(side: 'BUY' | 'SELL', level: number, seed: string): string {
  const s = side === 'BUY' ? 'B' : 'S';
  return `grid-${s}-${level}-${seed}`.slice(0, 50);
}

/** Price one step below reference (for BUY level i) */
function buyLevelPrice(ref: Decimal, spacing: Decimal, spacingMode: 'percent' | 'absolute', i: number): Decimal {
  if (spacingMode === 'percent') {
    const factor = new Decimal(1).minus(spacing.div(100));
    return ref.mul(factor.pow(i));
  }
  return ref.minus(spacing.mul(i));
}

/** Price one step above reference (for SELL level i) */
function sellLevelPrice(ref: Decimal, spacing: Decimal, spacingMode: 'percent' | 'absolute', i: number): Decimal {
  if (spacingMode === 'percent') {
    const factor = new Decimal(1).plus(spacing.div(100));
    return ref.mul(factor.pow(i));
  }
  return ref.plus(spacing.mul(i));
}

/**
 * Build the initial grid.
 * - long_only:  N BUY levels below ref
 * - short_only: N SELL levels above ref
 * - neutral:    N BUY levels below + N SELL levels above
 *
 * When dynamicSizing is enabled, quantity is calculated from available balance.
 * Otherwise, uses the fixed quantityPerLevel from config.
 */
export function buildGridLevels(
  config: BotConfig,
  referencePrice: Decimal,
  constraints: PairConstraints,
  seed: string,
  availableBalance?: Decimal  // Required when dynamicSizing is true
): GridLevel[] {
  // Compute spacing from gridRangePercent (for neutral) or use spacingValue (for directional)
  const spacing = config.gridRangePercent
    ? new Decimal(config.gridRangePercent).div(2).div(config.levels) // Linear spacing
    : new Decimal(config.spacingValue!);

  // Calculate quantity — dynamic or fixed
  let qty: Decimal;
  if (config.dynamicSizing && availableBalance) {
    qty = calculateDynamicQuantity(config, availableBalance, referencePrice, constraints);
  } else {
    qty = new Decimal(config.quantityPerLevel);
  }

  const levels: GridLevel[] = [];

  const makeBuy = (i: number): GridLevel => {
    const rawPrice = buyLevelPrice(referencePrice, spacing, config.spacingMode, i);
    return {
      level: i,
      side: 'BUY',
      price: rawPrice,
      priceStr: priceToString(rawPrice, constraints.tickSize),
      quantity: qty,
      quantityStr: qtyToString(qty, constraints.baseDecimalPlaces),
      customerOrderId: buildCOID('BUY', i, seed),
    };
  };

  const makeSell = (i: number): GridLevel => {
    const rawPrice = sellLevelPrice(referencePrice, spacing, config.spacingMode, i);
    return {
      level: i,
      side: 'SELL',
      price: rawPrice,
      priceStr: priceToString(rawPrice, constraints.tickSize),
      quantity: qty,
      quantityStr: qtyToString(qty, constraints.baseDecimalPlaces),
      customerOrderId: buildCOID('SELL', i, seed),
    };
  };

  if (config.mode === 'long_only' || config.mode === 'neutral') {
    for (let i = 1; i <= config.levels; i++) levels.push(makeBuy(i));
  }
  if (config.mode === 'short_only' || config.mode === 'neutral') {
    for (let i = 1; i <= config.levels; i++) levels.push(makeSell(i));
  }

  return levels;
}

/**
 * Build the replenishment order after a fill in neutral mode.
 *
 * When a BUY fills → place a new BUY one level deeper (further below ref)
 * When a SELL fills → place a new SELL one level deeper (further above ref)
 *
 * "nextLevel" = current deepest active level for that side + 1
 *
 * When dynamicSizing is enabled, quantity is recalculated from current balance.
 */
export function buildReplenishLevel(
  config: BotConfig,
  referencePrice: Decimal,
  constraints: PairConstraints,
  side: 'BUY' | 'SELL',
  nextLevel: number,
  seed: string,
  availableBalance?: Decimal  // Required when dynamicSizing is true
): GridLevel {
  // Compute spacing from gridRangePercent (for neutral) or use spacingValue (for directional)
  const spacing = config.gridRangePercent
    ? new Decimal(config.gridRangePercent).div(2).div(config.levels) // Linear spacing
    : new Decimal(config.spacingValue!);

  // Calculate quantity — dynamic or fixed
  let qty: Decimal;
  if (config.dynamicSizing && availableBalance) {
    qty = calculateDynamicQuantity(config, availableBalance, referencePrice, constraints);
  } else {
    qty = new Decimal(config.quantityPerLevel);
  }

  let rawPrice: Decimal;
  if (side === 'BUY') {
    rawPrice = buyLevelPrice(referencePrice, spacing, config.spacingMode, nextLevel);
  } else {
    rawPrice = sellLevelPrice(referencePrice, spacing, config.spacingMode, nextLevel);
  }

  return {
    level: nextLevel,
    side,
    price: rawPrice,
    priceStr: priceToString(rawPrice, constraints.tickSize),
    quantity: qty,
    quantityStr: qtyToString(qty, constraints.baseDecimalPlaces),
    customerOrderId: buildCOID(side, nextLevel, seed),
  };
}

/**
 * Calculate SL trigger price from averageEntryPrice.
 *
 * For neutral mode, net position can be long or short depending on which
 * side has filled more. Caller passes actual position side.
 *
 * long (net long position): SL below entry
 * sell (net short position): SL above entry
 */
export function calcSlPrice(
  config: BotConfig,
  averageEntryPrice: Decimal,
  positionSide?: 'buy' | 'sell'  // from exchange position — overrides config.mode for neutral
): Decimal {
  const value = new Decimal(config.stopLossValue);

  // Determine effective direction
  const isLong = positionSide
    ? positionSide === 'buy'
    : config.mode === 'long_only';

  if (config.stopLossMode === 'percent') {
    if (isLong) {
      return averageEntryPrice.mul(new Decimal(1).minus(value.div(100)));
    } else {
      return averageEntryPrice.mul(new Decimal(1).plus(value.div(100)));
    }
  } else {
    if (isLong) {
      return averageEntryPrice.minus(value);
    } else {
      return averageEntryPrice.plus(value);
    }
  }
}

/**
 * Calculate TP trigger price for a given position.
 * Returns null if tpMode === 'disabled' or mode is neutral (grid is its own TP).
 */
export function calcTpPrice(
  config: BotConfig,
  averageEntryPrice: Decimal,
  spacing: Decimal | undefined,
  positionSide?: 'buy' | 'sell'
): Decimal | null {
  // In neutral mode, the grid levels are the TP — no separate TP conditional needed
  if (config.mode === 'neutral') return null;
  if (config.tpMode === 'disabled') return null;
  if (!spacing) return null; // Safety check

  const isLong = positionSide
    ? positionSide === 'buy'
    : config.mode === 'long_only';

  if (config.tpMode === 'one_level') {
    return isLong
      ? averageEntryPrice.plus(spacing)
      : averageEntryPrice.minus(spacing);
  }

  if (config.tpMode === 'fixed') {
    const dist = new Decimal(config.tpFixedValue!);
    return isLong
      ? averageEntryPrice.plus(dist)
      : averageEntryPrice.minus(dist);
  }

  return null;
}

/** Get the grid spacing as an absolute Decimal amount (for TP calculation) */
export function getSpacingAmount(config: BotConfig, referencePrice: Decimal): Decimal {
  // Compute spacing from gridRangePercent (for neutral) or use spacingValue (for directional)
  const spacing = config.gridRangePercent
    ? new Decimal(config.gridRangePercent).div(2).div(config.levels) // Linear spacing as percent
    : new Decimal(config.spacingValue!);
  
  if (config.spacingMode === 'percent') {
    return referencePrice.mul(spacing.div(100));
  }
  return spacing;
}
