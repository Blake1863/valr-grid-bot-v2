/**
 * Grid level generator.
 * Pure functions — no I/O, testable in isolation.
 *
 * long_only:  laddered BUY orders below reference price
 * short_only: laddered SELL orders above reference price
 *
 * neutral (symmetric):
 *   N BUY orders below ref price  — the sells above are their take profits
 *   N SELL orders above ref price — the buys below are their take profits
 *   On fill → replenish that side with a new order one level deeper
 *   Price oscillating through grid = profit captured on each crossing
 *
 * percent spacing example (3 levels, 0.4%, ref=100):
 *   BUY  level 1: 100 * 0.996   = 99.60
 *   BUY  level 2: 100 * 0.996²  = 99.20
 *   BUY  level 3: 100 * 0.996³  = 98.81
 *   SELL level 1: 100 * 1.004   = 100.40
 *   SELL level 2: 100 * 1.004²  = 100.80
 *   SELL level 3: 100 * 1.004³  = 101.21
 *
 * absolute spacing example (3 levels, $0.50, ref=100):
 *   BUY  1: 99.50  BUY  2: 99.00  BUY  3: 98.50
 *   SELL 1: 100.50 SELL 2: 101.00 SELL 3: 101.50
 */

import Decimal from 'decimal.js';
import { priceToString, qtyToString } from '../exchange/pairMetadata.js';
import type { PairConstraints } from '../exchange/pairMetadata.js';
import type { BotConfig } from '../config/schema.js';

export interface GridLevel {
  level: number;       // 1-indexed distance from reference
  side: 'BUY' | 'SELL';
  price: Decimal;
  priceStr: string;    // rounded to tickSize
  quantity: Decimal;
  quantityStr: string; // truncated to baseDecimalPlaces
  customerOrderId: string;
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
 */
export function buildGridLevels(
  config: BotConfig,
  referencePrice: Decimal,
  constraints: PairConstraints,
  seed: string
): GridLevel[] {
  const spacing = new Decimal(config.spacingValue);
  const qty = new Decimal(config.quantityPerLevel);
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
 */
export function buildReplenishLevel(
  config: BotConfig,
  referencePrice: Decimal,
  constraints: PairConstraints,
  side: 'BUY' | 'SELL',
  nextLevel: number,
  seed: string
): GridLevel {
  const spacing = new Decimal(config.spacingValue);
  const qty = new Decimal(config.quantityPerLevel);

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
  spacing: Decimal,
  positionSide?: 'buy' | 'sell'
): Decimal | null {
  // In neutral mode, the grid levels are the TP — no separate TP conditional needed
  if (config.mode === 'neutral') return null;
  if (config.tpMode === 'disabled') return null;

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
  const spacing = new Decimal(config.spacingValue);
  if (config.spacingMode === 'percent') {
    return referencePrice.mul(spacing.div(100));
  }
  return spacing;
}
