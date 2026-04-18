/**
 * Grid Manager — Adaptive grid with fixed range.
 * 
 * Core behavior:
 * - Fixed price range: reference ± (gridRangePercent/2)
 * - N price levels per side (N bids below ref, N asks above ref)
 * - Bids placed ONLY below current price
 * - Asks placed ONLY above current price
 * - Order count adapts to price position within range
 * - When price at top of range: many bids, few asks
 * - When price at bottom of range: few bids, many asks
 * - Replenishment fills missing levels within range bounds
 * - NO recenter — grid range is fixed forever
 */

import Decimal from 'decimal.js';
import type { BotConfig } from '../config/schema.js';
import type { PairConstraints } from '../exchange/pairMetadata.js';
import { priceToString, qtyToString } from '../exchange/pairMetadata.js';
import { createLogger } from '../app/logger.js';

const log = createLogger('gridManager');

export type LegState = 'missing' | 'active' | 'filled';

export interface GridLevel {
  levelIndex: number;         // 1 to N (distance from reference)
  bidPrice: Decimal;
  bidPriceStr: string;
  askPrice: Decimal;
  askPriceStr: string;
  bidLeg: {
    customerOrderId: string;
    exchangeOrderId?: string;
    state: LegState;
    quantity: Decimal;
    quantityStr: string;
    filledAt?: string;
  };
  askLeg: {
    customerOrderId: string;
    exchangeOrderId?: string;
    state: LegState;
    quantity: Decimal;
    quantityStr: string;
    filledAt?: string;
  };
}

export interface GridState {
  levels: GridLevel[];
  referencePrice: Decimal;
  minBidPrice: Decimal;       // Lowest bid (level N)
  maxAskPrice: Decimal;       // Highest ask (level N)
  currentPrice: Decimal;
  activeBidCount: number;
  activeAskCount: number;
}

/** Build customerOrderId for a leg — max 50 chars */
function buildCOID(side: 'BUY' | 'SELL', level: number, seed: string): string {
  const s = side === 'BUY' ? 'B' : 'S';
  return `grid-${s}-${level}-${seed}`.slice(0, 50);
}

/**
 * Compute spacing per level from total grid range.
 * Linear spacing for predictable bounds.
 */
export function computeSpacingFromRange(config: BotConfig): Decimal {
  const halfRange = new Decimal(config.gridRangePercent).div(2);
  return halfRange.div(config.levels);
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

  const totalLevels = config.levels * 2;
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
 * Build the grid levels with fixed price points.
 * Does NOT determine which orders to place — that depends on current price.
 */
export function buildGridLevels(
  config: BotConfig,
  referencePrice: Decimal,
  constraints: PairConstraints,
  quantity: Decimal,
  seed: string
): GridLevel[] {
  const spacingPercent = computeSpacingFromRange(config); // e.g., 0.167 for 0.167%
  const levels: GridLevel[] = [];

  for (let i = 1; i <= config.levels; i++) {
    // Apply spacing as percentage of reference price
    const bidPrice = referencePrice.mul(new Decimal(1).minus(spacingPercent.div(100).mul(i)));
    const askPrice = referencePrice.mul(new Decimal(1).plus(spacingPercent.div(100).mul(i)));

    levels.push({
      levelIndex: i,
      bidPrice,
      bidPriceStr: priceToString(bidPrice, constraints.tickSize),
      askPrice,
      askPriceStr: priceToString(askPrice, constraints.tickSize),
      bidLeg: {
        customerOrderId: buildCOID('BUY', i, seed),
        state: 'missing',
        quantity,
        quantityStr: qtyToString(quantity, constraints.baseDecimalPlaces),
      },
      askLeg: {
        customerOrderId: buildCOID('SELL', i, seed),
        state: 'missing',
        quantity,
        quantityStr: qtyToString(quantity, constraints.baseDecimalPlaces),
      },
    });
  }

  return levels;
}

/**
 * Initialize grid state with current price.
 * Determines which levels should have active orders.
 */
export function initGridState(
  config: BotConfig,
  referencePrice: Decimal,
  currentPrice: Decimal,
  constraints: PairConstraints,
  availableBalance: Decimal,
  seed: string
): GridState {
  const quantity = config.dynamicSizing
    ? calculateQuantityPerLevel(config, availableBalance, referencePrice, constraints)
    : new Decimal(config.quantityPerLevel);

  const levels = buildGridLevels(config, referencePrice, constraints, quantity, seed);

  const spacingPercent = computeSpacingFromRange(config);
  // Calculate min/max as percentage of reference
  const totalRangePercent = spacingPercent.mul(config.levels);
  const minBidPrice = referencePrice.mul(new Decimal(1).minus(totalRangePercent.div(100)));
  const maxAskPrice = referencePrice.mul(new Decimal(1).plus(totalRangePercent.div(100)));

  const state: GridState = {
    levels,
    referencePrice,
    minBidPrice,
    maxAskPrice,
    currentPrice,
    activeBidCount: 0,
    activeAskCount: 0,
  };

  // Mark which legs should be active based on current price
  updateGridOrders(state, currentPrice);

  return state;
}

/**
 * Update which orders should be active based on current price.
 * 
 * Rules:
 * - Bid at level i is active if: currentPrice > bidPrice
 * - Ask at level i is active if: currentPrice < askPrice
 * 
 * This means:
 * - Price at top of range → many bids, few asks
 * - Price at bottom of range → few bids, many asks
 * - Price in middle → ~equal bids and asks
 */
export function updateGridOrders(grid: GridState, currentPrice: Decimal): void {
  grid.currentPrice = currentPrice;
  let activeBids = 0;
  let activeAsks = 0;

  for (const level of grid.levels) {
    // Bid should be active if current price is ABOVE the bid price
    const bidShouldBeActive = currentPrice.gt(level.bidPrice);
    // Ask should be active if current price is BELOW the ask price
    const askShouldBeActive = currentPrice.lt(level.askPrice);

    // Update bid state (only if not already filled)
    if (level.bidLeg.state !== 'filled') {
      if (bidShouldBeActive && level.bidLeg.state === 'missing') {
        level.bidLeg.state = 'active';
      } else if (!bidShouldBeActive && level.bidLeg.state === 'active') {
        // Price moved above this bid - order would have filled or been cancelled
        // Mark as missing so it can be replaced if appropriate
        level.bidLeg.state = 'missing';
        level.bidLeg.exchangeOrderId = undefined;
      }
    }

    // Update ask state (only if not already filled)
    if (level.askLeg.state !== 'filled') {
      if (askShouldBeActive && level.askLeg.state === 'missing') {
        level.askLeg.state = 'active';
      } else if (!askShouldBeActive && level.askLeg.state === 'active') {
        level.askLeg.state = 'missing';
        level.askLeg.exchangeOrderId = undefined;
      }
    }

    if (level.bidLeg.state === 'active') activeBids++;
    if (level.askLeg.state === 'active') activeAsks++;
  }

  grid.activeBidCount = activeBids;
  grid.activeAskCount = activeAsks;
}

/**
 * Mark a leg as filled.
 */
export function markLegFilled(
  grid: GridState,
  customerOrderId: string | undefined,
  exchangeOrderId: string
): { level: GridLevel; side: 'BUY' | 'SELL' } | null {
  for (const level of grid.levels) {
    if (level.bidLeg.exchangeOrderId === exchangeOrderId || 
        level.bidLeg.customerOrderId === customerOrderId) {
      level.bidLeg.state = 'filled';
      level.bidLeg.filledAt = new Date().toISOString();
      log.info({ level: level.levelIndex, side: 'BUY', price: level.bidPriceStr }, 'Bid filled');
      return { level, side: 'BUY' };
    }
    if (level.askLeg.exchangeOrderId === exchangeOrderId ||
        level.askLeg.customerOrderId === customerOrderId) {
      level.askLeg.state = 'filled';
      level.askLeg.filledAt = new Date().toISOString();
      log.info({ level: level.levelIndex, side: 'SELL', price: level.askPriceStr }, 'Ask filled');
      return { level, side: 'SELL' };
    }
  }
  return null;
}

/**
 * Get legs that need to be placed (state = 'active' but no exchangeOrderId).
 */
export function getLegsToPlace(grid: GridState): Array<{
  level: GridLevel;
  side: 'BUY' | 'SELL';
  price: Decimal;
  priceStr: string;
  quantity: Decimal;
  quantityStr: string;
  customerOrderId: string;
}> {
  const toPlace: Array<{
    level: GridLevel;
    side: 'BUY' | 'SELL';
    price: Decimal;
    priceStr: string;
    quantity: Decimal;
    quantityStr: string;
    customerOrderId: string;
  }> = [];

  for (const level of grid.levels) {
    if (level.bidLeg.state === 'active' && !level.bidLeg.exchangeOrderId) {
      toPlace.push({
        level,
        side: 'BUY',
        price: level.bidPrice,
        priceStr: level.bidPriceStr,
        quantity: level.bidLeg.quantity,
        quantityStr: level.bidLeg.quantityStr,
        customerOrderId: level.bidLeg.customerOrderId,
      });
    }
    if (level.askLeg.state === 'active' && !level.askLeg.exchangeOrderId) {
      toPlace.push({
        level,
        side: 'SELL',
        price: level.askPrice,
        priceStr: level.askPriceStr,
        quantity: level.askLeg.quantity,
        quantityStr: level.askLeg.quantityStr,
        customerOrderId: level.askLeg.customerOrderId,
      });
    }
  }

  return toPlace;
}

/**
 * Get legs to cancel (have exchangeOrderId but state is no longer 'active').
 */
export function getLegsToCancel(grid: GridState): Array<{
  level: GridLevel;
  side: 'BUY' | 'SELL';
  exchangeOrderId: string;
}> {
  const toCancel: Array<{
    level: GridLevel;
    side: 'BUY' | 'SELL';
    exchangeOrderId: string;
  }> = [];

  for (const level of grid.levels) {
    if (level.bidLeg.exchangeOrderId && level.bidLeg.state !== 'active' && level.bidLeg.state !== 'filled') {
      toCancel.push({
        level,
        side: 'BUY',
        exchangeOrderId: level.bidLeg.exchangeOrderId,
      });
    }
    if (level.askLeg.exchangeOrderId && level.askLeg.state !== 'active' && level.askLeg.state !== 'filled') {
      toCancel.push({
        level,
        side: 'SELL',
        exchangeOrderId: level.askLeg.exchangeOrderId,
      });
    }
  }

  return toCancel;
}

/**
 * Log grid state for observability.
 */
export function logGridState(grid: GridState, config: BotConfig): void {
  log.info(
    {
      referencePrice: grid.referencePrice.toString(),
      currentPrice: grid.currentPrice.toString(),
      rangeMin: grid.minBidPrice.toString(),
      rangeMax: grid.maxAskPrice.toString(),
      activeBids: grid.activeBidCount,
      activeAsks: grid.activeAskCount,
      totalLevels: config.levels,
      pricePosition: grid.currentPrice.minus(grid.minBidPrice).div(grid.maxAskPrice.minus(grid.minBidPrice)).mul(100).toFixed(1) + '%',
    },
    'Grid state summary'
  );
}
