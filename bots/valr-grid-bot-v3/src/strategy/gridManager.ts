/**
 * Grid Manager — OKX/Bybit Neutral Mode
 * 
 * Core behavior matching exchange futures grid bots:
 * 
 * 1. GRID RANGE
 *    - Operates only inside [lowerBound, upperBound]
 *    - If price exits range: stop new entries, manage existing positions
 *    - Resume when price re-enters range
 * 
 * 2. NEUTRAL MODE
 *    - Below reference price: place BUY orders
 *    - Above reference price: place SELL orders
 *    - After BUY fill: place SELL at nearest grid above
 *    - After SELL fill: place BUY at nearest grid below
 * 
 * 3. GRID CYCLES
 *    - Each completed cycle: buy@L[i] → sell@L[i+1] OR sell@L[i] → buy@L[i-1]
 *    - Track realized profit per cycle
 *    - Separate from unrealized PnL on open positions
 * 
 * 4. ORDER PLACEMENT
 *    - On startup: seed orders based on current/reference price
 *    - On fill: immediately place adjacent completion order
 *    - On price move: adjust active orders to stay nearest to price
 */

import { Decimal } from 'decimal.js';
import type { BotConfig } from '../config/schema.js';
import type { PairConstraints } from '../exchange/pairMetadata.js';
import { createLogger } from '../app/logger.js';
import type { GridLevel, GridConstruction } from './gridBuilder.js';
import { getAdjacentTarget } from './gridBuilder.js';

const log = createLogger('gridManager');

export type OrderRole = 'entry' | 'exit';

export interface GridOrder {
  levelIndex: number;
  side: 'BUY' | 'SELL';
  price: Decimal;
  priceStr: string;
  quantity: Decimal;
  quantityStr: string;
  customerOrderId: string;
  exchangeOrderId?: string;
  state: 'missing' | 'pending' | 'active' | 'filled' | 'cancelled';
  role: OrderRole;
  adjacentTargetIndex?: number;  // Where to place completion order after fill
  filledAt?: string;
  fillPrice?: Decimal;
  cycleProfit?: Decimal;         // Realized profit from this cycle
}

export interface GridCycle {
  cycleId: string;
  entryLevelIndex: number;
  exitLevelIndex: number;
  entrySide: 'BUY' | 'SELL';
  entryPrice: Decimal;
  exitPrice: Decimal;
  quantity: Decimal;
  realizedProfit: Decimal;
  completedAt: string;
}

export interface GridState {
  construction: GridConstruction;
  referencePrice: Decimal;
  currentPrice: Decimal;
  orders: Map<number, GridOrder>;    // levelIndex → order
  activeOrderIds: Set<string>;        // customerOrderIds currently active on exchange
  completedCycles: GridCycle[];
  totalRealizedProfit: Decimal;
  inRange: boolean;                   // Is current price inside [lower, upper]?
  dataHealthy: boolean;               // Is price data fresh?
}

/**
 * Initialize grid state on startup.
 */
export function initGridState(
  config: BotConfig,
  construction: GridConstruction,
  referencePrice: Decimal,
  currentPrice: Decimal,
  constraints: PairConstraints,
  quantity: Decimal,
  seed: string
): GridState {
  const orders = new Map<number, GridOrder>();
  const inRange = currentPrice.greaterThanOrEqualTo(construction.lowerBound) &&
                  currentPrice.lessThanOrEqualTo(construction.upperBound);

  // Assign sides and quantities to levels (skip boundaries)
  for (let i = 1; i < construction.levels.length - 1; i++) {
    const level = construction.levels[i];
    const price = level.price;
    
    // Determine side based on reference price
    let side: 'BUY' | 'SELL' | undefined;
    let adjacentTargetIndex: number | undefined;
    
    if (price.lt(referencePrice)) {
      side = 'BUY';
      adjacentTargetIndex = i + 1; // Sell at next level up after fill
    } else if (price.gt(referencePrice)) {
      side = 'SELL';
      adjacentTargetIndex = i - 1; // Buy at next level down after fill
    }
    
    if (side) {
      orders.set(i, {
        levelIndex: i,
        side,
        price,
        priceStr: level.priceStr,
        quantity,
        quantityStr: qtyToString(quantity, constraints.baseDecimalPlaces),
        customerOrderId: buildCOID(config.mode, side, i, seed),
        state: 'missing',
        role: 'entry',
        adjacentTargetIndex,
      });
    }
  }

  return {
    construction,
    referencePrice,
    currentPrice,
    orders,
    activeOrderIds: new Set(),
    completedCycles: [],
    totalRealizedProfit: new Decimal(0),
    inRange,
    dataHealthy: true,
  };
}

/**
 * Check if price is inside trading range.
 */
export function checkInRange(
  currentPrice: Decimal,
  lowerBound: Decimal,
  upperBound: Decimal
): boolean {
  return currentPrice.greaterThanOrEqualTo(lowerBound) &&
         currentPrice.lessThanOrEqualTo(upperBound);
}

/**
 * Get orders that should be active based on current price.
 * 
 * OKX/Bybit behavior:
 * - Only place orders inside the range
 * - If price outside range: no new entries (but manage existing)
 */
export function getOrdersToPlace(
  state: GridState,
  config: BotConfig,
  currentPrice: Decimal
): GridOrder[] {
  const toPlace: GridOrder[] = [];
  
  // If outside range, don't place new entry orders
  if (!state.inRange) {
    log.warn({ currentPrice: currentPrice.toString() }, 'Price outside range — skipping new entry placement');
    return toPlace;
  }
  
  // If data is stale, don't place new orders
  if (!state.dataHealthy) {
    log.warn('Price data stale — skipping new entry placement');
    return toPlace;
  }

  for (const [levelIdx, order] of state.orders) {
    // Skip if already active/pending/filled
    if (order.state !== 'missing' && order.state !== 'cancelled') {
      continue;
    }
    
    // Skip if exchange order already exists
    if (order.exchangeOrderId) {
      continue;
    }

    // Determine if this order should be active based on current price
    const shouldBeActive = shouldOrderBeActive(order, currentPrice, state.referencePrice, state.inRange);
    
    if (shouldBeActive) {
      toPlace.push(order);
    }
  }

  // Respect max active orders limit
  const currentActive = state.activeOrderIds.size;
  const maxNew = config.maxActiveGridOrders - currentActive;
  
  if (maxNew <= 0) {
    return [];
  }

  return toPlace.slice(0, maxNew);
}

/**
 * Determine if an order should be active based on price position.
 */
function shouldOrderBeActive(
  order: GridOrder,
  currentPrice: Decimal,
  referencePrice: Decimal,
  inRange: boolean
): boolean {
  // Entry orders: follow reference price logic
  if (order.role === 'entry') {
    if (order.side === 'BUY') {
      return order.price.lt(currentPrice);
    } else {
      return order.price.gt(currentPrice);
    }
  }
  
  // Exit orders (completion orders): always active if counterpart filled
  if (order.role === 'exit') {
    return true;
  }
  
  return false;
}

/**
 * Get orders that should be cancelled.
 */
export function getOrdersToCancel(state: GridState, currentPrice: Decimal): GridOrder[] {
  const toCancel: GridOrder[] = [];

  for (const [levelIdx, order] of state.orders) {
    if (order.state !== 'active' && order.state !== 'pending') {
      continue;
    }

    const shouldBeActive = shouldOrderBeActive(order, currentPrice, state.referencePrice, state.inRange);
    
    if (!shouldBeActive && order.role === 'entry') {
      toCancel.push(order);
    }
  }

  return toCancel;
}

/**
 * Handle order fill — implement adjacent-level cycle logic.
 * 
 * OKX/Bybit neutral mode:
 * - Buy at L[i] fills → create/activate sell order at L[i+1]
 * - Sell at L[i] fills → create/activate buy order at L[i-1]
 */
export function handleOrderFill(
  state: GridState,
  config: BotConfig,
  filledOrder: GridOrder,
  fillPrice: Decimal,
  fillQuantity: Decimal,
  constraints: PairConstraints
): { completionOrder?: GridOrder; cycle?: GridCycle } {
  log.info(
    { level: filledOrder.levelIndex, side: filledOrder.side, price: fillPrice.toString() },
    'Order filled'
  );

  // Mark order as filled
  filledOrder.state = 'filled';
  filledOrder.filledAt = new Date().toISOString();
  filledOrder.fillPrice = fillPrice;
  state.activeOrderIds.delete(filledOrder.customerOrderId);

  // Get adjacent target level
  const targetIdx = getAdjacentTarget(filledOrder.levelIndex, filledOrder.side, state.construction.levels);
  
  if (targetIdx === null) {
    log.warn({ level: filledOrder.levelIndex }, 'No adjacent target — at range boundary');
    return {};
  }

  // Check if target order exists
  let targetOrder = state.orders.get(targetIdx);
  
  if (!targetOrder) {
    // Create new completion order at target level
    const targetLevel = state.construction.levels[targetIdx];
    const oppositeSide: 'BUY' | 'SELL' = filledOrder.side === 'BUY' ? 'SELL' : 'BUY';
    
    targetOrder = {
      levelIndex: targetIdx,
      side: oppositeSide,
      price: targetLevel.price,
      priceStr: targetLevel.priceStr,
      quantity: fillQuantity,  // Same quantity as filled order
      quantityStr: qtyToString(fillQuantity, constraints.baseDecimalPlaces),
      customerOrderId: buildCOID(config.mode, oppositeSide, targetIdx, 'cycle'),
      state: 'missing',
      role: 'exit',  // This is a completion order
      adjacentTargetIndex: filledOrder.levelIndex,  // Cycle back if this fills
    };
    
    state.orders.set(targetIdx, targetOrder);
    log.info(
      { targetLevel: targetIdx, side: oppositeSide, price: targetLevel.priceStr },
      'Created completion order'
    );
  } else {
    // Existing order — this completes a cycle
    if (targetOrder.state === 'filled') {
      // Both legs filled — calculate cycle profit
      const cycle = completeGridCycle(state, config, filledOrder, targetOrder);
      return { cycle };
    }
  }

  return { completionOrder: targetOrder };
}

/**
 * Record a completed grid cycle.
 */
function completeGridCycle(
  state: GridState,
  config: BotConfig,
  order1: GridOrder,
  order2: GridOrder
): GridCycle {
  // Determine entry and exit
  const entry = order1.side === 'BUY' ? order1 : order2;
  const exit = order1.side === 'BUY' ? order2 : order1;
  
  // Calculate profit
  let profit: Decimal;
  if (entry.side === 'BUY') {
    // Long cycle: buy low, sell high
    profit = exit.fillPrice!.minus(entry.fillPrice!).mul(exit.quantity);
  } else {
    // Short cycle: sell high, buy low
    profit = entry.fillPrice!.minus(exit.fillPrice!).mul(entry.quantity);
  }
  
  // Subtract fees (estimate)
  const feeRate = new Decimal('0.0005');  // 0.05% per side (adjust based on VALR fees)
  const fees = entry.fillPrice!.mul(entry.quantity).mul(feeRate)
    .plus(exit.fillPrice!.mul(exit.quantity).mul(feeRate));
  
  profit = profit.minus(fees);

  const cycle: GridCycle = {
    cycleId: `cycle-${Date.now()}-${entry.levelIndex}`,
    entryLevelIndex: entry.levelIndex,
    exitLevelIndex: exit.levelIndex,
    entrySide: entry.side,
    entryPrice: entry.fillPrice!,
    exitPrice: exit.fillPrice!,
    quantity: entry.quantity,
    realizedProfit: profit,
    completedAt: new Date().toISOString(),
  };

  state.completedCycles.push(cycle);
  state.totalRealizedProfit = state.totalRealizedProfit.plus(profit);

  log.info(
    {
      cycleId: cycle.cycleId,
      entryLevel: entry.levelIndex,
      exitLevel: exit.levelIndex,
      profit: profit.toString(),
    },
    'Grid cycle completed'
  );

  // Reset orders for next cycle
  entry.state = 'missing';
  entry.exchangeOrderId = undefined;
  entry.fillPrice = undefined;
  entry.filledAt = undefined;
  
  exit.state = 'missing';
  exit.exchangeOrderId = undefined;
  exit.fillPrice = undefined;
  exit.filledAt = undefined;

  return cycle;
}

/**
 * Update grid state when price changes.
 */
export function updateGridState(
  state: GridState,
  config: BotConfig,
  currentPrice: Decimal
): { inRange: boolean; toPlace: GridOrder[]; toCancel: GridOrder[] } {
  const wasInRange = state.inRange;
  state.currentPrice = currentPrice;
  state.inRange = checkInRange(currentPrice, state.construction.lowerBound, state.construction.upperBound);

  if (wasInRange !== state.inRange) {
    log.info(
      { inRange: state.inRange, price: currentPrice.toString() },
      state.inRange ? 'Price re-entered range — resuming grid' : 'Price exited range — pausing new entries'
    );
  }

  const toPlace = getOrdersToPlace(state, config, currentPrice);
  const toCancel = getOrdersToCancel(state, currentPrice);

  return { inRange: state.inRange, toPlace, toCancel };
}

/**
 * Mark order as active (placed on exchange).
 */
export function markOrderActive(state: GridState, customerOrderId: string, exchangeOrderId: string): boolean {
  for (const order of state.orders.values()) {
    if (order.customerOrderId === customerOrderId) {
      order.state = 'active';
      order.exchangeOrderId = exchangeOrderId;
      state.activeOrderIds.add(customerOrderId);
      return true;
    }
  }
  return false;
}

/**
 * Mark order as cancelled.
 */
export function markOrderCancelled(state: GridState, customerOrderId: string): boolean {
  for (const order of state.orders.values()) {
    if (order.customerOrderId === customerOrderId) {
      order.state = 'cancelled';
      order.exchangeOrderId = undefined;
      state.activeOrderIds.delete(customerOrderId);
      return true;
    }
  }
  return false;
}

/**
 * Set data health status.
 */
export function setDataHealthy(state: GridState, healthy: boolean): void {
  state.dataHealthy = healthy;
  if (!healthy) {
    log.warn('Price data stale — entering degraded mode');
  }
}

// Helper functions
function buildCOID(mode: string, side: 'BUY' | 'SELL', level: number, seed: string): string {
  const m = mode === 'neutral' ? 'N' : mode === 'long' ? 'L' : 'S';
  const s = side === 'BUY' ? 'B' : 'S';
  return `grid-${m}-${s}${level}-${seed}`.slice(0, 50);
}

function qtyToString(qty: Decimal, decimalPlaces: number): string {
  return qty.toFixed(decimalPlaces);
}
