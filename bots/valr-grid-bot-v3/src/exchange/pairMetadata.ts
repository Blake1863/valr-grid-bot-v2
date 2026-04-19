/**
 * Pair Metadata — VALR Perpetual Futures
 * 
 * Provides tick size, quantity precision, and min order constraints.
 */

import Decimal from 'decimal.js';

export interface PairConstraints {
  pair: string;
  tickSize: Decimal;
  baseDecimalPlaces: number;
  quoteDecimalPlaces: number;
  minBaseAmount: string;
  minQuoteAmount: string;
}

// Known constraints for common perpetual pairs
const PAIR_CONSTRAINTS: Record<string, PairConstraints> = {
  SOLUSDTPERP: {
    pair: 'SOLUSDTPERP',
    tickSize: new Decimal('0.01'),
    baseDecimalPlaces: 2,
    quoteDecimalPlaces: 2,
    minBaseAmount: '0.01',
    minQuoteAmount: '1',
  },
  ETHUSDTPERP: {
    pair: 'ETHUSDTPERP',
    tickSize: new Decimal('0.1'),
    baseDecimalPlaces: 3,
    quoteDecimalPlaces: 1,
    minBaseAmount: '0.001',
    minQuoteAmount: '1',
  },
  BTCUSDTPERP: {
    pair: 'BTCUSDTPERP',
    tickSize: new Decimal('1'),
    baseDecimalPlaces: 4,
    quoteDecimalPlaces: 0,
    minBaseAmount: '0.0001',
    minQuoteAmount: '1',
  },
};

/**
 * Get constraints for a pair.
 */
export function getPairConstraints(pair: string): PairConstraints {
  const constraints = PAIR_CONSTRAINTS[pair.toUpperCase()];
  if (!constraints) {
    // Default constraints for unknown pairs
    return {
      pair,
      tickSize: new Decimal('0.01'),
      baseDecimalPlaces: 2,
      quoteDecimalPlaces: 2,
      minBaseAmount: '0.01',
      minQuoteAmount: '1',
    };
  }
  return constraints;
}

/**
 * Round price to tick size.
 */
export function roundToTick(price: Decimal, tickSize: Decimal): Decimal {
  const invTick = new Decimal(1).div(tickSize);
  return price.mul(invTick).floor().div(invTick);
}

/**
 * Format price as string respecting tick size.
 */
export function priceToString(price: Decimal, tickSize: Decimal): string {
  // Determine decimal places from tick size
  const tickStr = tickSize.toString();
  const decimalPart = tickStr.split('.')[1];
  const decimals = decimalPart ? decimalPart.length : 0;
  return price.toFixed(decimals);
}

/**
 * Format quantity as string respecting precision.
 */
export function qtyToString(qty: Decimal, decimalPlaces: number): string {
  return qty.toFixed(decimalPlaces);
}

/**
 * Validate order meets minimum requirements.
 */
export function validateOrder(
  price: Decimal,
  quantity: Decimal,
  constraints: PairConstraints
): { valid: boolean; error?: string } {
  const notional = price.mul(quantity);
  const minQty = new Decimal(constraints.minBaseAmount);
  const minNotional = new Decimal(constraints.minQuoteAmount);

  if (quantity.lessThan(minQty)) {
    return {
      valid: false,
      error: `Quantity ${quantity.toString()} below minimum ${minQty}`,
    };
  }

  if (notional.lessThan(minNotional)) {
    return {
      valid: false,
      error: `Notional ${notional.toString()} below minimum ${minNotional}`,
    };
  }

  return { valid: true };
}
