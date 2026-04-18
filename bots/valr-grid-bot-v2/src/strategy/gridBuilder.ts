/**
 * Grid Builder — Legacy support for directional modes only.
 * 
 * For bounded price-range grid with N total orders, use gridManager.ts.
 * 
 * This file provides calcSlPrice() for TPSL calculations.
 */

import Decimal from 'decimal.js';
import type { BotConfig } from '../config/schema.js';

/**
 * Calculate SL trigger price from averageEntryPrice.
 * 
 * long (net long position): SL below entry
 * sell (net short position): SL above entry
 */
export function calcSlPrice(
  config: BotConfig,
  averageEntryPrice: Decimal,
  positionSide?: 'buy' | 'sell'
): Decimal {
  const value = new Decimal(config.stopLossValue);

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
 * Calculate TP trigger price — returns null for neutral mode.
 */
export function calcTpPrice(
  config: BotConfig,
  averageEntryPrice: Decimal,
  _spacing: Decimal | undefined,
  positionSide?: 'buy' | 'sell'
): Decimal | null {
  // In neutral mode, the grid levels are the TP — no separate TP conditional needed
  if (config.mode === 'neutral') return null;
  if (config.tpMode === 'disabled') return null;

  const isLong = positionSide
    ? positionSide === 'buy'
    : config.mode === 'long_only';

  if (config.tpMode === 'fixed') {
    const dist = new Decimal(config.tpFixedValue!);
    return isLong
      ? averageEntryPrice.plus(dist)
      : averageEntryPrice.minus(dist);
  }

  return null;
}

// Deprecated functions — use gridManager.ts instead
export function buildGridLevels() {
  throw new Error('Deprecated — use gridManager.ts');
}

export function buildReplenishLevel() {
  throw new Error('Deprecated — use gridManager.ts');
}

export function getSpacingAmount() {
  throw new Error('Deprecated — use gridManager.ts');
}

export function calculateDynamicQuantity() {
  throw new Error('Deprecated — use gridManager.ts');
}

export interface GridLevel {
  levelIndex: number;
  side: 'BUY' | 'SELL';
  price: Decimal;
  priceStr: string;
  quantity: Decimal;
  quantityStr: string;
  customerOrderId: string;
}
