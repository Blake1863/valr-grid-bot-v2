/**
 * Startup reconciliation logic.
 * Exchange state is always authoritative.
 *
 * Process:
 * 1. Fetch exchange positions and open orders
 * 2. Compare to local DB state
 * 3. Resolve discrepancies:
 *    - Exchange has position, local is flat → rebuild from exchange
 *    - Local has orders that exchange doesn't → mark stale
 *    - Exchange has orders outside strategy intent → identify orphans
 * 4. Return reconciled state for main loop to act on
 */

import Decimal from 'decimal.js';
import type { ValrRestClient } from '../exchange/restClient.js';
import type { StateStore } from '../state/store.js';
import type { PositionManager } from './positionManager.js';
import type { BotConfig } from '../config/schema.js';
import { createLogger } from '../app/logger.js';

const log = createLogger('reconciliation');

export interface ReconcileResult {
  /** Whether a live position exists on exchange */
  hasPosition: boolean;
  /** Whether grid orders need to be placed from scratch */
  needsGridPlacement: boolean;
  /** Whether a TPSL needs to be placed */
  needsTpsl: boolean;
  /** Active grid orders found on exchange that match our customerOrderIds */
  liveOrderCount: number;
  /** Orphaned exchange order IDs to cancel */
  orphanedOrderIds: string[];
}

export async function reconcile(
  rest: ValrRestClient,
  store: StateStore,
  positionManager: PositionManager,
  config: BotConfig
): Promise<ReconcileResult> {
  log.info({ pair: config.pair }, 'Starting startup reconciliation');

  // 1. Fetch exchange positions
  const exchangePositions = await rest.getOpenPositions(config.pair);
  positionManager.updateFromRest(exchangePositions);

  // 2. Fetch all open orders (VALR ignores pair filter — filter client-side)
  const allOpenOrders = await rest.getOpenOrders();
  const pairOrders = allOpenOrders.filter(
    (o) => o.currencyPair?.toUpperCase() === config.pair.toUpperCase()
  );

  log.info(
    {
      exchangePositionCount: exchangePositions.length,
      pairOpenOrders: pairOrders.length,
    },
    'Exchange state fetched'
  );

  // 3. Get local active orders
  const localActiveOrders = store.getActiveGridOrders(config.pair);
  const localCustomerIds = new Set(localActiveOrders.map((o) => o.customerOrderId));
  const localExchangeIds = new Set(
    localActiveOrders.filter((o) => o.exchangeOrderId).map((o) => o.exchangeOrderId!)
  );

  // 4. Find orphaned exchange orders (exchange has them but local doesn't know about them)
  const orphanedOrderIds: string[] = [];
  for (const exOrder of pairOrders) {
    const matchByExchangeId = localExchangeIds.has(exOrder.orderId);
    const matchByCustomerId = exOrder.customerOrderId
      ? localCustomerIds.has(exOrder.customerOrderId)
      : false;

    if (!matchByExchangeId && !matchByCustomerId) {
      // Check if it looks like a grid order we placed (customerOrderId prefix)
      if (exOrder.customerOrderId?.startsWith('grid-')) {
        // Known grid format — probably from previous run, trust it
        // Parse level from customerOrderId: grid-B-3-seed or grid-S-2-seed or grid-L-1-seed
        const levelMatch = exOrder.customerOrderId.match(/^grid-[A-Z]-?(\d+)-/);
        const level = levelMatch ? parseInt(levelMatch[1], 10) : 0;
        log.info(
          { orderId: exOrder.orderId, customerOrderId: exOrder.customerOrderId, level },
          'Found grid order from previous run — restoring to local state'
        );
        const now = new Date().toISOString();
        store.upsertGridOrder({
          pair: config.pair,
          side: exOrder.side.toUpperCase() as 'BUY' | 'SELL',
          price: exOrder.price,
          quantity: exOrder.remainingQuantity,
          customerOrderId: exOrder.customerOrderId,
          exchangeOrderId: exOrder.orderId,
          status: 'placed',
          level,
          createdAt: exOrder.createdAt,
          updatedAt: now,
        });
      } else {
        // Unknown order — orphaned
        log.warn(
          { orderId: exOrder.orderId, customerOrderId: exOrder.customerOrderId },
          'Orphaned order found — will cancel'
        );
        orphanedOrderIds.push(exOrder.orderId);
      }
    }
  }

  // 5. Find local orders that are no longer on exchange → mark stale
  for (const localOrder of localActiveOrders) {
    if (!localOrder.exchangeOrderId) continue;
    const stillOnExchange = pairOrders.some((o) => o.orderId === localOrder.exchangeOrderId);
    if (!stillOnExchange) {
      log.info(
        { customerOrderId: localOrder.customerOrderId },
        'Local order no longer on exchange — marking cancelled'
      );
      store.updateGridOrderStatus(localOrder.customerOrderId, 'cancelled');
    }
  }

  const hasPosition = !positionManager.isFlat;
  const reconciledActive = store.getActiveGridOrders(config.pair);

  // For neutral mode, grid should hedge the position: position + (sells - buys) ≈ 0
  let needsGridPlacement: boolean;
  if (config.mode === 'neutral') {
    const activeBuys = reconciledActive.filter((o) => o.side === 'BUY').length;
    const activeSells = reconciledActive.filter((o) => o.side === 'SELL').length;
    const qtyPerLevel = new Decimal(config.quantityPerLevel);
    
    // In neutral mode: we want (position + buyOrders - sellOrders) = 0
    // Rearranged: sellOrders - buyOrders = position
    // So if long 0.55 SOL with 0.15 SOL orders: need ~4 more sells than buys
    const positionQty = positionManager.netQuantity.abs();
    const ordersNeededToHedge = positionQty.div(qtyPerLevel).toNumber();
    
    const currentHedge = activeSells - activeBuys; // positive = net short orders
    const isLong = positionManager.netQuantity.gt(0);
    
    // If long position: need (sells - buys) = positionQty
    // If short position: need (buys - sells) = positionQty.abs()
    let targetHedge = isLong ? ordersNeededToHedge : -ordersNeededToHedge;
    
    // Also want full grid on both sides for proper oscillation
    needsGridPlacement = activeBuys < config.levels || activeSells < config.levels || Math.abs(currentHedge - targetHedge) > 0.5;
    
    if (needsGridPlacement) {
      log.info(
        { 
          activeBuys, 
          activeSells,
          currentHedge,
          targetHedge: targetHedge.toFixed(2),
          positionSide: isLong ? 'LONG' : 'SHORT',
          positionQty: positionQty.toString(),
          required: config.levels 
        }, 
        'Neutral mode: grid needs rebalancing'
      );
    }
  } else {
    needsGridPlacement = reconciledActive.length < config.levels;
  }

  const hasTpsl = !!store.getTpsl(config.pair);
  const needsTpsl = hasPosition && !hasTpsl;

  log.info(
    {
      hasPosition,
      needsGridPlacement,
      needsTpsl,
      liveOrderCount: reconciledActive.length,
      orphanedCount: orphanedOrderIds.length,
    },
    'Reconciliation complete'
  );

  return {
    hasPosition,
    needsGridPlacement,
    needsTpsl,
    liveOrderCount: reconciledActive.length,
    orphanedOrderIds,
  };
}
