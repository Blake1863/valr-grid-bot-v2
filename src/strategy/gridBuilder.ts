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
 * For long_only/short_only: always returns a TP price (never null in directional modes).
 * For neutral mode: returns null (uses symmetric grid orders as closing mechanism).
 */
export function calcTpPrice(
  config: BotConfig,
  averageEntryPrice: Decimal,
  spacing: Decimal,
  positionSide?: 'buy' | 'sell'
): Decimal | null {
  // In neutral mode, the grid levels serve as closing orders — no separate TP conditional needed
  if (config.mode === 'neutral') return null;

  const isLong = positionSide
    ? positionSide === 'buy'
    : config.mode === 'long_only';

  // For directional modes, always calculate a TP price
  // Default to one_level spacing if tpMode is disabled
  if (config.tpMode === 'disabled') {
    // Use grid spacing as default TP distance
    return isLong
      ? averageEntryPrice.plus(spacing)
      : averageEntryPrice.minus(spacing);
  }

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

/**
 * Build symmetric closing orders for neutral mode.
 *
 * When a position has a net long exposure (more BUYs filled than SELLs),
 * create SELL orders above the average entry price to close the excess.
 *
 * When a position has a net short exposure (more SELLs filled than BUYs),
 * create BUY orders below the average entry price to close the excess.
 *
 * The closing orders mirror the entry grid structure but on the opposite side.
 */
export function buildClosingOrders(
  config: BotConfig,
  referencePrice: Decimal,
  averageEntryPrice: Decimal,
  constraints: PairConstraints,
  quantity: Decimal,  // net position quantity to close
  seed: string
): GridLevel[] {
  if (config.mode !== 'neutral' || quantity.isZero()) {
    return [];
  }

  const spacing = new Decimal(config.spacingValue);
  const closingOrders: GridLevel[] = [];

  // Determine if we're closing a long (net positive) or short (net negative) position
  const isClosingLong = quantity.gt(0);
  const closingQty = quantity.abs();
  const closingQtyStr = qtyToString(closingQty, constraints.baseDecimalPlaces);

  // Number of closing orders to distribute across
  const numClosingOrders = Math.min(config.levels, Math.ceil(closingQty.div(new Decimal(config.quantityPerLevel)).toNumber()));

  // For net long position: place SELL orders above entry (closing side)
  // For net short position: place BUY orders below entry (closing side)
  for (let i = 1; i <= numClosingOrders; i++) {
    let closingPrice: Decimal;
    let closingSide: 'BUY' | 'SELL';

    if (isClosingLong) {
      // Closing a long: sell above entry
      closingSide = 'SELL';
      closingPrice = sellLevelPrice(averageEntryPrice, spacing, config.spacingMode, i);
    } else {
      // Closing a short: buy below entry
      closingSide = 'BUY';
      closingPrice = buyLevelPrice(averageEntryPrice, spacing, config.spacingMode, i);
    }

    closingOrders.push({
      level: i,
      side: closingSide,
      price: closingPrice,
      priceStr: priceToString(closingPrice, constraints.tickSize),
      quantity: closingQty.div(numClosingOrders),
      quantityStr: qtyToString(closingQty.div(numClosingOrders), constraints.baseDecimalPlaces),
      customerOrderId: buildCOID(closingSide, i, `close-${seed}`).slice(0, 50),
    });
  }

  return closingOrders;
}
