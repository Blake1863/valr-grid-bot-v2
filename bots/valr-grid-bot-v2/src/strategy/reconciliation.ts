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
  /** Whether to cancel ALL grid orders and rebuild fresh (for asymmetric grids) */
  needsFullRebuild?: boolean;
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
  // Also detect duplicate orders at same level — keep only the most recent
  const orphanedOrderIds: string[] = [];
  const ordersByLevelSide = new Map<string, Array<typeof pairOrders[number]>>();
  
  for (const exOrder of pairOrders) {
    const matchByExchangeId = localExchangeIds.has(exOrder.orderId);
    const matchByCustomerId = exOrder.customerOrderId
      ? localCustomerIds.has(exOrder.customerOrderId)
      : false;

    if (!matchByExchangeId && !matchByCustomerId) {
      // Check if it looks like a grid order we placed (customerOrderId prefix)
      if (exOrder.customerOrderId?.startsWith('grid-')) {
        // Known grid format — probably from previous run
        // Parse level from customerOrderId: grid-B-3-seed or grid-S-2-seed or grid-L-1-seed
        const levelMatch = exOrder.customerOrderId.match(/^grid-[A-Z]-?(\d+)-/);
        const level = levelMatch ? parseInt(levelMatch[1], 10) : 0;
        const side = exOrder.side.toUpperCase() as 'BUY' | 'SELL';
        const key = `${side}-${level}`;
        
        // Group by level+side to detect duplicates
        if (!ordersByLevelSide.has(key)) {
          ordersByLevelSide.set(key, []);
        }
        ordersByLevelSide.get(key)!.push(exOrder);
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
  
  // Process grouped orders — keep only the most recent at each level
  for (const [key, orders] of ordersByLevelSide.entries()) {
    if (orders.length === 1) {
      // Single order — just restore it
      const exOrder = orders[0];
      const levelMatch = exOrder.customerOrderId!.match(/^grid-[A-Z]-?(\d+)-/);
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
        customerOrderId: exOrder.customerOrderId || `restored-${exOrder.orderId}`,
        exchangeOrderId: exOrder.orderId,
        status: 'placed',
        level,
        createdAt: exOrder.createdAt,
        updatedAt: now,
      });
    } else {
      // Duplicates found — keep the most recent (by createdAt), cancel the rest
      orders.sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime());
      const keep = orders[0];
      const cancel = orders.slice(1);
      
      const levelMatch = keep.customerOrderId!.match(/^grid-[A-Z]-?(\d+)-/);
      const level = levelMatch ? parseInt(levelMatch[1], 10) : 0;
      log.info(
        { orderId: keep.orderId, customerOrderId: keep.customerOrderId, level },
        'Found grid order from previous run — restoring to local state'
      );
      const now = new Date().toISOString();
      store.upsertGridOrder({
        pair: config.pair,
        side: keep.side.toUpperCase() as 'BUY' | 'SELL',
        price: keep.price,
        quantity: keep.remainingQuantity,
        customerOrderId: keep.customerOrderId || `restored-${keep.orderId}`,
        exchangeOrderId: keep.orderId,
        status: 'placed',
        level,
        createdAt: keep.createdAt,
        updatedAt: now,
      });
      
      for (const dup of cancel) {
        log.warn(
          { orderId: dup.orderId, customerOrderId: dup.customerOrderId, level, reason: 'duplicate at same level' },
          'Duplicate grid order found — will cancel'
        );
        orphanedOrderIds.push(dup.orderId);
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

  // 5b. Deduplicate orders at same level — keep only the most recent
  const reconciledActive = store.getActiveGridOrders(config.pair);
  const localOrdersByLevelSide = new Map<string, Array<typeof reconciledActive[number]>>();
  
  for (const order of reconciledActive) {
    const key = `${order.side}-${order.level}`;
    if (!localOrdersByLevelSide.has(key)) {
      localOrdersByLevelSide.set(key, []);
    }
    localOrdersByLevelSide.get(key)!.push(order);
  }
  
  for (const [key, orders] of localOrdersByLevelSide.entries()) {
    if (orders.length > 1) {
      // Duplicates found — keep the most recent (by updatedAt), cancel the rest
      orders.sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime());
      const keep = orders[0];
      const cancel = orders.slice(1);
      
      for (const dup of cancel) {
        log.warn(
          { 
            customerOrderId: dup.customerOrderId, 
            exchangeOrderId: dup.exchangeOrderId,
            level: dup.level,
            side: dup.side,
            reason: 'duplicate at same level'
          },
          'Duplicate grid order found in local state — will cancel'
        );
        if (dup.exchangeOrderId) {
          orphanedOrderIds.push(dup.exchangeOrderId);
        }
        store.updateGridOrderStatus(dup.customerOrderId, 'cancelled');
      }
    }
  }

  const hasPosition = !positionManager.isFlat;
  // reconciledActive already fetched in step 5b for deduplication

  // For neutral mode, grid should hedge the position: position + (sells - buys) ≈ 0
  let needsGridPlacement: boolean;
  let needsFullRebuild = false; // Flag to cancel all and start fresh
  
  if (config.mode === 'neutral') {
    const activeBuys = reconciledActive.filter((o) => o.side === 'BUY').length;
    const activeSells = reconciledActive.filter((o) => o.side === 'SELL').length;
    const qtyPerLevel = new Decimal(config.quantityPerLevel);
    
    // In neutral mode: we want (position + buyOrders - sellOrders) = 0
    // Rearranged: sellOrders - buyOrders = position
    const positionQty = positionManager.netQuantity.abs();
    const ordersNeededToHedge = positionQty.div(qtyPerLevel).toNumber();
    
    const currentHedge = activeSells - activeBuys; // positive = net short orders
    const isLong = positionManager.netQuantity.gt(0);
    
    // If long position: need (sells - buys) = positionQty
    // If short position: need (buys - sells) = positionQty.abs()
    let targetHedge = isLong ? ordersNeededToHedge : -ordersNeededToHedge;
    
    // CRITICAL FIX: Check for asymmetric grid with no position — needs full rebuild
    // If there's no position but orders exist on only one side, the grid is stale
    // and should be cancelled + replaced with a fresh symmetric grid
    if (!hasPosition && (activeBuys > 0 || activeSells > 0)) {
      if (activeBuys === 0 || activeSells === 0 || activeBuys !== activeSells) {
        needsFullRebuild = true;
        log.warn(
          { activeBuys, activeSells, reason: 'asymmetric grid with no position' },
          'Neutral mode: stale asymmetric grid detected — will cancel all and rebuild fresh'
        );
      }
      
      // Also check for orders from different seeds (indicates stale orders from multiple runs)
      const seeds = new Set(reconciledActive.map(o => {
        const match = o.customerOrderId.match(/-([^-]+)$/);
        return match ? match[1] : 'unknown';
      }));
      if (seeds.size > 1) {
        needsFullRebuild = true;
        log.warn(
          { seeds: Array.from(seeds), orderCount: reconciledActive.length, reason: 'mixed seeds from different runs' },
          'Neutral mode: grid has orders from multiple runs — will cancel all and rebuild fresh'
        );
      }
    }
    
    // Only check hedge balance if we have a position
    // If no position, we want symmetric grid (buys = sells)
    if (hasPosition) {
      needsGridPlacement = Math.abs(currentHedge - targetHedge) > 0.5;
    } else {
      // No position: want symmetric grid
      needsGridPlacement = activeBuys !== activeSells || activeBuys < config.levels;
    }
    
    if (needsGridPlacement && !needsFullRebuild) {
      log.info(
        { 
          activeBuys, 
          activeSells,
          currentHedge,
          targetHedge: targetHedge.toFixed(2),
          positionSide: isLong ? 'LONG' : 'SHORT',
          positionQty: positionQty.toString(),
          hedgeImbalance: Math.abs(currentHedge - targetHedge).toFixed(2)
        }, 
        'Neutral mode: grid needs rebalancing (hedge-focused)'
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
      needsFullRebuild,
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
    needsFullRebuild,
  };
}
