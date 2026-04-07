/**
 * Pair metadata loader — loads, validates, and caches pair constraints.
 * All price/qty rounding must use the values from here, not hardcoded.
 */

import Decimal from 'decimal.js';
import type { ValrPairInfo } from './types.js';
import type { ValrRestClient } from './restClient.js';
import { createLogger } from '../app/logger.js';

const log = createLogger('pairMetadata');

export interface PairConstraints {
  symbol: string;
  tickSize: Decimal;
  baseDecimalPlaces: number;
  minBaseAmount: Decimal;
  maxBaseAmount: Decimal;
  minQuoteAmount: Decimal;
  maxQuoteAmount: Decimal;
  initialMarginFraction: Decimal;
  maintenanceMarginFraction: Decimal;
  autoCloseMarginFraction: Decimal;
}

export function roundToTickSize(price: Decimal, tickSize: Decimal): Decimal {
  // Round down to nearest tick
  return price.div(tickSize).floor().mul(tickSize);
}

export function truncateToBaseDecimals(qty: Decimal, baseDecimalPlaces: number): Decimal {
  return qty.toDecimalPlaces(baseDecimalPlaces, Decimal.ROUND_DOWN);
}

export function priceToString(price: Decimal, tickSize: Decimal): string {
  // Format with enough decimal places to represent the tick size
  const tickDecimals = tickSize.decimalPlaces() ?? 0;
  return roundToTickSize(price, tickSize).toFixed(tickDecimals);
}

export function qtyToString(qty: Decimal, baseDecimalPlaces: number): string {
  return truncateToBaseDecimals(qty, baseDecimalPlaces).toFixed(baseDecimalPlaces);
}

export class PairMetadataLoader {
  private rest: ValrRestClient;
  private cache: Map<string, PairConstraints> = new Map();

  constructor(rest: ValrRestClient) {
    this.rest = rest;
  }

  async load(pair: string): Promise<PairConstraints> {
    if (this.cache.has(pair)) {
      return this.cache.get(pair)!;
    }

    log.info({ pair }, 'Loading pair metadata');
    const pairs = await this.rest.getPairsByType('FUTURE');
    const info: ValrPairInfo | undefined = pairs.find(
      (p) => p.symbol === pair && p.active
    );

    if (!info) {
      throw new Error(
        `Pair ${pair} not found in FUTURE pairs or is inactive. Available: ${pairs
          .filter((p) => p.active)
          .map((p) => p.symbol)
          .join(', ')}`
      );
    }

    const constraints: PairConstraints = {
      symbol: info.symbol,
      tickSize: new Decimal(info.tickSize),
      baseDecimalPlaces: parseInt(info.baseDecimalPlaces, 10),
      minBaseAmount: new Decimal(info.minBaseAmount),
      maxBaseAmount: new Decimal(info.maxBaseAmount),
      minQuoteAmount: new Decimal(info.minQuoteAmount),
      maxQuoteAmount: new Decimal(info.maxQuoteAmount),
      initialMarginFraction: new Decimal(info.initialMarginFraction ?? '0.1'),
      maintenanceMarginFraction: new Decimal(info.maintenanceMarginFraction ?? '0.05'),
      autoCloseMarginFraction: new Decimal(info.autoCloseMarginFraction ?? '0.033'),
    };

    log.info(
      {
        pair,
        tickSize: constraints.tickSize.toString(),
        baseDecimalPlaces: constraints.baseDecimalPlaces,
        minBaseAmount: constraints.minBaseAmount.toString(),
      },
      'Pair metadata loaded'
    );

    this.cache.set(pair, constraints);
    return constraints;
  }

  validateOrderQty(qty: Decimal, constraints: PairConstraints): void {
    if (qty.lt(constraints.minBaseAmount)) {
      throw new Error(
        `Order quantity ${qty} is below minimum ${constraints.minBaseAmount} for ${constraints.symbol}`
      );
    }
    if (qty.gt(constraints.maxBaseAmount)) {
      throw new Error(
        `Order quantity ${qty} exceeds maximum ${constraints.maxBaseAmount} for ${constraints.symbol}`
      );
    }
  }
}
