/**
 * Grid order manager — places, tracks, cancels grid limit orders.
 *
 * Uses batch orders for initial grid placement (up to 20 per batch).
 * Tracks orders in SQLite and reconciles against exchange state.
 */

import Decimal from 'decimal.js';
import type { ValrRestClient } from '../exchange/restClient.js';
import type { StateStore } from '../state/store.js';
import type { BotConfig } from '../config/schema.js';
import type { PairConstraints } from '../exchange/pairMetadata.js';
import type { GridLevel } from './gridBuilder.js';
import type { WsOrderStatusUpdate } from '../exchange/types.js';
import { createLogger } from '../app/logger.js';

const log = createLogger('orderManager');

export class OrderManager {
  private rest: ValrRestClient;
  private store: StateStore;
  private config: BotConfig;
  private constraints: PairConstraints;
  private dryRun: boolean;
  private failureCount = 0;
  private circuitOpen = false;
  private circuitOpenAt = 0;
  private readonly CIRCUIT_BREAKER_THRESHOLD = 3;
  private readonly CIRCUIT_BREAKER_COOLDOWN_MS = 60_000;

  constructor(
    rest: ValrRestClient,
    store: StateStore,
    config: BotConfig,
    constraints: PairConstraints
  ) {
    this.rest = rest;
    this.store = store;
    this.config = config;
    this.constraints = constraints;
    this.dryRun = config.dryRun;
  }

  /** Place entire grid via batch API */
  async placeGrid(levels: GridLevel[]): Promise<void> {
    if (levels.length === 0) return;
    this._checkCircuitBreaker();

    const now = new Date().toISOString();

    // Register orders in DB as 'pending' first
    for (const level of levels) {
      this.store.upsertGridOrder({
        pair: this.config.pair,
        side: level.side,
        price: level.priceStr,
        quantity: level.quantityStr,
        customerOrderId: level.customerOrderId,
        exchangeOrderId: null,
        status: 'pending',
        level: level.level,
        createdAt: now,
        updatedAt: now,
      });
    }

    if (this.dryRun) {
      log.info({ count: levels.length }, '[DRY-RUN] Would place grid via batch');
      for (const level of levels) {
        this.store.updateGridOrderStatus(level.customerOrderId, 'placed', `dry-${level.customerOrderId}`);
      }
      return;
    }

    // Batch in chunks of 20 (API limit)
    for (let i = 0; i < levels.length; i += 20) {
      const chunk = levels.slice(i, i + 20);
      const requests = chunk.map((level) => ({
        type: 'PLACE_LIMIT' as const,
        data: {
          pair: this.config.pair,
          side: level.side,
          quantity: level.quantityStr,
          price: level.priceStr,
          timeInForce: 'GTC',
          postOnly: this.config.postOnly,
          customerOrderId: level.customerOrderId,
        },
      }));

      const seed = Date.now().toString(36);
      try {
        const result = await this.rest.placeBatchOrders({
          customerBatchId: `grid-batch-${seed}`,
          requests,
        });

        for (let j = 0; j < result.outcomes.length; j++) {
          const outcome = result.outcomes[j];
          const level = chunk[j];
          if (outcome.accepted && outcome.orderId) {
            this.store.updateGridOrderStatus(level.customerOrderId, 'placed', outcome.orderId);
            log.info(
              { level: level.level, price: level.priceStr, orderId: outcome.orderId },
              'Grid order accepted'
            );
            this.failureCount = 0;
          } else {
            this.store.updateGridOrderStatus(level.customerOrderId, 'failed');
            log.error(
              { level: level.level, price: level.priceStr, error: outcome.error },
              'Grid order rejected in batch'
            );
            this._recordFailure();
          }
        }
      } catch (err) {
        log.error({ err }, 'Batch order placement failed');
        for (const level of chunk) {
          this.store.updateGridOrderStatus(level.customerOrderId, 'failed');
        }
        this._recordFailure();
      }
    }
  }

  /** Place a single grid level (for replenishment after fill) */
  async placeSingleOrder(level: GridLevel & { pairId?: string }): Promise<void> {
    this._checkCircuitBreaker();

    const now = new Date().toISOString();
    this.store.upsertGridOrder({
      pair: this.config.pair,
      side: level.side,
      price: level.priceStr,
      quantity: level.quantityStr,
      customerOrderId: level.customerOrderId,
      exchangeOrderId: null,
      status: 'pending',
      level: level.level,
      pairId: level.pairId,
      createdAt: now,
      updatedAt: now,
    });

    if (this.dryRun) {
      log.info({ level: level.level, price: level.priceStr }, '[DRY-RUN] Would place single grid order');
      this.store.updateGridOrderStatus(level.customerOrderId, 'placed', `dry-${level.customerOrderId}`);
      return;
    }

    try {
      const resp = await this.rest.placeLimitOrder({
        side: level.side,
        quantity: level.quantityStr,
        price: level.priceStr,
        pair: this.config.pair,
        postOnly: this.config.postOnly,
        customerOrderId: level.customerOrderId,
        timeInForce: 'GTC',
        reduceOnly: false,
      });
      this.store.updateGridOrderStatus(level.customerOrderId, 'placed', resp.id);
      this.failureCount = 0;
      log.info({ level: level.level, price: level.priceStr, orderId: resp.id }, 'Single grid order placed');
    } catch (err) {
      this.store.updateGridOrderStatus(level.customerOrderId, 'failed');
      this._recordFailure();
      throw err;
    }
  }

  /** Cancel all resting grid orders for this pair */
  async cancelAll(): Promise<void> {
    if (this.dryRun) {
      log.info('[DRY-RUN] Would cancel all grid orders');
      this.store.clearGridOrdersForPair(this.config.pair);
      return;
    }
    // CRITICAL FIX: Always clear local DB state even if REST call fails
    // This prevents orphaned orders persisting after restart
    try {
      await this.rest.cancelAllOrdersForPair(this.config.pair);
    } catch (err) {
      log.error({ err }, 'Failed to cancel all grid orders on exchange — clearing DB anyway');
      // Don't rethrow — still clear local state to avoid stale orders on restart
    }
    this.store.clearGridOrdersForPair(this.config.pair);
    log.info({ pair: this.config.pair }, 'All grid orders cancelled (DB cleared)');
  }

  /**
   * Look up the grid order for a WS update BEFORE marking it as filled.
   * Used by neutral mode to know which side was filled.
   */
  getFilledOrderData(data: WsOrderStatusUpdate): { side: 'BUY' | 'SELL'; price: string; level: number } | null {
    if (data.orderStatusType !== 'Filled') return null;
    const order = this.store.getGridOrderByExchangeId(data.orderId) ??
      (data.customerOrderId ? this.store.getGridOrderByCustomerId(data.customerOrderId) : undefined);
    if (!order) return null;
    return { side: order.side, price: order.price, level: order.level };
  }

  /** Handle ORDER_STATUS_UPDATE from WS */
  handleOrderStatusUpdate(data: WsOrderStatusUpdate): 'filled' | 'cancelled' | 'failed' | 'other' {
    const order = this.store.getGridOrderByExchangeId(data.orderId) ??
      (data.customerOrderId ? this.store.getGridOrderByCustomerId(data.customerOrderId) : undefined);

    if (!order) {
      // Not a grid order we placed
      return 'other';
    }

    switch (data.orderStatusType) {
      case 'Filled':
        this.store.updateGridOrderStatus(order.customerOrderId, 'filled');
        log.info({ level: order.level, price: order.price, orderId: data.orderId }, 'Grid order filled');
        return 'filled';

      case 'Cancelled':
        this.store.updateGridOrderStatus(order.customerOrderId, 'cancelled');
        log.info({ level: order.level, price: order.price }, 'Grid order cancelled');
        return 'cancelled';

      case 'Failed':
        this.store.updateGridOrderStatus(order.customerOrderId, 'failed');
        log.warn({ level: order.level, reason: data.failedReason }, 'Grid order failed');
        this._recordFailure();
        return 'failed';

      case 'Placed':
        // Confirm placement from exchange
        if (!order.exchangeOrderId && data.orderId) {
          this.store.updateGridOrderStatus(order.customerOrderId, 'placed', data.orderId);
        }
        return 'other';

      default:
        return 'other';
    }
  }

  getActiveOrders(): ReturnType<StateStore['getActiveGridOrders']> {
    return this.store.getActiveGridOrders(this.config.pair);
  }

  isCircuitOpen(): boolean {
    if (!this.circuitOpen) return false;
    if (Date.now() - this.circuitOpenAt > this.CIRCUIT_BREAKER_COOLDOWN_MS) {
      log.info('Circuit breaker reset after cooldown');
      this.circuitOpen = false;
      this.failureCount = 0;
      return false;
    }
    return true;
  }

  private _recordFailure(): void {
    this.failureCount++;
    if (this.failureCount >= this.CIRCUIT_BREAKER_THRESHOLD) {
      log.error(
        { failureCount: this.failureCount },
        'CIRCUIT BREAKER OPEN — too many consecutive order failures'
      );
      this.circuitOpen = true;
      this.circuitOpenAt = Date.now();
    }
  }

  private _checkCircuitBreaker(): void {
    if (this.isCircuitOpen()) {
      throw new Error('Circuit breaker is open — order placement halted');
    }
  }
}
