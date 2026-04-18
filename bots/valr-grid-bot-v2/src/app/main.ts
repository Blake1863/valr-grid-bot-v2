/**
 * VALR Grid Bot v2 — Main entry point
 *
 * Symmetric grid bot with explicit pair tracking.
 * 
 * Core behavior:
 * - Maintains N bid + N ask levels within configured range (no expansion)
 * - Tracks grid as explicit pairs (bid + ask at each level)
 * - Replaces completed pairs to keep grid full
 * - Allows natural long/short exposure from grid trading
 * - Recenter grid when price drifts beyond threshold
 *
 * Startup sequence:
 * 1. Load + validate config
 * 2. Fetch API credentials from secrets manager
 * 3. Load pair metadata (tick size, decimals, margin fractions)
 * 4. Fetch account balances
 * 5. Startup reconciliation (exchange state is authoritative)
 * 6. Cancel orphaned orders found during reconciliation
 * 7. Connect WebSocket clients
 * 8. Place grid orders + TPSL if needed
 * 9. Enter event loop
 */

import { execSync } from 'child_process';
import Decimal from 'decimal.js';
import { loadConfig } from '../config/loader.js';
import { ValrRestClient } from '../exchange/restClient.js';
import { PairMetadataLoader } from '../exchange/pairMetadata.js';
import { WsAccountClient } from '../exchange/wsAccountClient.js';
import { WsTradeClient } from '../exchange/wsTradeClient.js';
import { StateStore } from '../state/store.js';
import { PositionManager } from '../strategy/positionManager.js';
import { TpslManager } from '../strategy/tpslManager.js';
import { OrderManager } from '../strategy/orderManager.js';
import { RiskManager } from '../strategy/riskManager.js';
import { reconcile } from '../strategy/reconciliation.js';
import { buildGridLevels } from '../strategy/gridBuilder.js';
import {
  buildGridState,
  getMissingLegs,
  getCompletedPairs,
  markLegActive,
  markLegFilled,
  markLegMissing,
  recalculateGridStats,
  replaceCompletedPair,
  logGridState,
  calculateQuantityPerLevel,
  computeSpacingFromRange,
  type GridState,
} from '../strategy/gridPairManager.js';
import type { WsOrderStatusUpdate, WsOpenPositionUpdate, WsPositionClosed, WsFailedOrder } from '../exchange/types.js';
import { createLogger } from './logger.js';

const log = createLogger('main');

// Global grid state for neutral mode
let gridState: GridState | null = null;

function getSecret(name: string): string {
  try {
    const val = execSync(
      `python3 /home/admin/.openclaw/secrets/secrets.py get ${name}`,
      { encoding: 'utf-8', timeout: 10000 }
    ).trim();
    if (!val) throw new Error(`Empty value for secret ${name}`);
    return val;
  } catch (err) {
    throw new Error(`Failed to fetch secret "${name}": ${err}`);
  }
}

async function getReferencePrice(
  config: ReturnType<typeof loadConfig>,
  tradeWs: WsTradeClient,
  rest: ValrRestClient
): Promise<Decimal> {
  if (config.referencePriceSource === 'manual') {
    return new Decimal(config.manualReferencePrice!);
  }

  // Try WS first
  if (config.referencePriceSource === 'mark_price' && tradeWs.markPrice) {
    return tradeWs.markPrice;
  }
  if (config.referencePriceSource === 'mid_price' && tradeWs.midPrice) {
    return tradeWs.midPrice;
  }
  if (config.referencePriceSource === 'last_traded' && tradeWs.lastTradedPrice) {
    return tradeWs.lastTradedPrice;
  }

  // Fallback to REST
  log.info('WS price not available — fetching from REST');
  const summary = await rest.getMarketSummary(config.pair);

  if (config.referencePriceSource === 'mark_price' && summary.markPrice) {
    return new Decimal(summary.markPrice);
  }
  if (config.referencePriceSource === 'mid_price' && summary.bidPrice && summary.askPrice) {
    return new Decimal(summary.bidPrice).plus(new Decimal(summary.askPrice)).div(2);
  }
  if (summary.lastTradedPrice) {
    return new Decimal(summary.lastTradedPrice);
  }

  throw new Error('Cannot determine reference price from any source');
}

/**
 * Place missing grid legs for neutral mode (pair-based).
 */
async function placeMissingGridLegs(
  grid: GridState,
  orderManager: OrderManager,
  config: ReturnType<typeof loadConfig>,
  constraints: Awaited<ReturnType<PairMetadataLoader['load']>>,
  availableBalance: Decimal
): Promise<void> {
  const missingLegs = getMissingLegs(grid);
  if (missingLegs.length === 0) {
    log.info('Grid is full — no missing legs to place');
    return;
  }

  log.info({ count: missingLegs.length }, 'Placing missing grid legs');

  // Find the pairId for each leg
  for (const leg of missingLegs) {
    const pair = grid.pairs.find(p => 
      p.bidLeg.customerOrderId === leg.customerOrderId || 
      p.askLeg.customerOrderId === leg.customerOrderId
    );
    
    try {
      const result = await orderManager.placeSingleOrder({
        level: leg.level,
        side: leg.side,
        price: leg.price,
        priceStr: leg.priceStr,
        quantity: leg.quantity,
        quantityStr: leg.quantityStr,
        customerOrderId: leg.customerOrderId,
        pairId: pair?.pairId,
      });
      // Mark as active - exchangeOrderId will be set by WS confirmation
      const activePair = grid.pairs.find(p => 
        p.bidLeg.customerOrderId === leg.customerOrderId || 
        p.askLeg.customerOrderId === leg.customerOrderId
      );
      if (activePair) {
        if (activePair.bidLeg.customerOrderId === leg.customerOrderId) {
          activePair.bidLeg.state = 'active';
        } else {
          activePair.askLeg.state = 'active';
        }
        activePair.updatedAt = new Date().toISOString();
        recalculateGridStats(grid);
      }
      log.info(
        { level: leg.level, side: leg.side, price: leg.priceStr },
        'Grid leg placed'
      );
    } catch (err) {
      log.error({ err, level: leg.level, side: leg.side }, 'Failed to place grid leg');
    }
  }

  recalculateGridStats(grid);
  logGridState(grid, config);
}

/**
 * Replace completed pairs to keep grid full.
 */
async function replaceCompletedPairs(
  grid: GridState,
  orderManager: OrderManager,
  config: ReturnType<typeof loadConfig>,
  constraints: Awaited<ReturnType<PairMetadataLoader['load']>>,
  availableBalance: Decimal
): Promise<void> {
  const completed = getCompletedPairs(grid);
  if (completed.length === 0) return;

  log.info({ count: completed.length }, 'Replacing completed grid pairs');

  const seed = Date.now().toString(36);

  for (const pair of completed) {
    const qty = config.dynamicSizing && availableBalance.gt(0)
      ? calculateQuantityPerLevel(config, availableBalance, grid.referencePrice, constraints)
      : new Decimal(config.quantityPerLevel);

    const newPair = replaceCompletedPair(
      grid,
      pair.pairId,
      config,
      grid.referencePrice,
      constraints,
      qty,
      seed
    );

    if (newPair) {
      // Place both legs of the new pair - state already set to 'missing' by replaceCompletedPair
      // WS ORDER_STATUS_UPDATE will mark them as 'active' when confirmed
      try {
        await orderManager.placeSingleOrder({
          level: newPair.bidLeg.level,
          side: newPair.bidLeg.side,
          price: newPair.bidLeg.price,
          priceStr: newPair.bidLeg.priceStr,
          quantity: newPair.bidLeg.quantity,
          quantityStr: newPair.bidLeg.quantityStr,
          customerOrderId: newPair.bidLeg.customerOrderId,
          pairId: newPair.pairId,
        });
      } catch (err) {
        log.error({ err, pair: pair.pairId, side: 'BUY' }, 'Failed to place replacement bid');
      }

      try {
        await orderManager.placeSingleOrder({
          level: newPair.askLeg.level,
          side: newPair.askLeg.side,
          price: newPair.askLeg.price,
          priceStr: newPair.askLeg.priceStr,
          quantity: newPair.askLeg.quantity,
          quantityStr: newPair.askLeg.quantityStr,
          customerOrderId: newPair.askLeg.customerOrderId,
          pairId: newPair.pairId,
        });
      } catch (err) {
        log.error({ err, pair: pair.pairId, side: 'SELL' }, 'Failed to place replacement ask');
      }
    }
  }

  recalculateGridStats(grid);
  logGridState(grid, config);
}

async function main(): Promise<void> {
  log.info('VALR Grid Bot v2 starting (symmetric grid with pair tracking)');

  // ─── 1. Load config ───────────────────────────────────────────────────────
  const config = loadConfig();

  if (config.dryRun) {
    log.warn('DRY-RUN MODE — no real orders will be placed');
  }

  if (config.mode !== 'neutral') {
    log.warn('Pair-based grid is optimized for neutral mode. Directional modes use legacy logic.');
  }

  // ─── 2. Credentials ───────────────────────────────────────────────────────
  log.info('Fetching API credentials');
  const apiKey = getSecret('valr_api_key');
  const apiSecret = getSecret('valr_api_secret');

  // ─── 3. Initialize clients ────────────────────────────────────────────────
  const rest = new ValrRestClient(apiKey, apiSecret, config.subaccountId);
  const store = new StateStore();
  const metadataLoader = new PairMetadataLoader(rest);

  // Load pair metadata — fail fast if pair invalid
  const constraints = await metadataLoader.load(config.pair);

  // ─── 4. Account balances ──────────────────────────────────────────────────
  const balances = await rest.getBalances();
  const usdtBalance = balances.find(
    (b) => b.currency === 'USDT' || b.currency === 'usdt'
  );
  const availableBalance = usdtBalance ? new Decimal(usdtBalance.available) : new Decimal(0);
  log.info(
    { available: usdtBalance?.available ?? 'N/A', total: usdtBalance?.total ?? 'N/A' },
    'Account USDT balance'
  );

  // ─── 5. Build strategy components ─────────────────────────────────────────
  const positionManager = new PositionManager(config.pair, store);
  positionManager.loadFromStore();

  const riskManager = new RiskManager(config, constraints);
  const tpslManager = new TpslManager(rest, config, constraints, store);
  const orderManager = new OrderManager(rest, store, config, constraints);

  // ─── 6. Startup reconciliation ────────────────────────────────────────────
  log.info('Running startup reconciliation');
  const reconcileResult = await reconcile(rest, store, positionManager, config);

  // Cancel orphaned orders
  for (const orderId of reconcileResult.orphanedOrderIds) {
    try {
      await rest.cancelOrder(orderId, config.pair);
    } catch (err) {
      log.warn({ orderId, err }, 'Failed to cancel orphaned order');
    }
  }

  // ─── 7. Connect WebSocket clients ─────────────────────────────────────────
  const tradeWs = new WsTradeClient(config.pair, config.wsStaleTimeoutSecs * 1000);
  tradeWs.connect();

  // Wait for trade WS to get initial price (up to 5s)
  await new Promise<void>((resolve) => {
    const start = Date.now();
    const check = setInterval(() => {
      if (tradeWs.bestPrice || Date.now() - start > 5000) {
        clearInterval(check);
        resolve();
      }
    }, 100);
  });

  const accountWs = new WsAccountClient(
    apiKey,
    apiSecret,
    {
      onOrderStatusUpdate: async (data: WsOrderStatusUpdate) => {
        const result = orderManager.handleOrderStatusUpdate(data);

        if (result === 'filled' && config.mode === 'neutral' && gridState) {
          try {
            // Mark the leg as filled in grid state
            markLegFilled(gridState, data.customerOrderId, data.orderId);

            // Refresh position from exchange (authoritative)
            const positions = await rest.getOpenPositions(config.pair);
            positionManager.updateFromRest(positions);

            const refPrice = await getReferencePrice(config, tradeWs, rest);

            // Check cooldown before any action
            try {
              riskManager.checkCooldown(store);
            } catch {
              log.info('In cooldown after position close — skipping replenishment');
              return;
            }

            // Replace completed pairs to keep grid full
            await replaceCompletedPairs(gridState, orderManager, config, constraints, availableBalance);

            // Place SL if we have a net position
            if (!positionManager.isFlat) {
              try {
                await tpslManager.rebuild(positionManager.position!, refPrice);
              } catch (err) {
                log.error({ err }, 'CRITICAL: SL placement failed after fill');
              }
            }

            logGridState(gridState, config);
          } catch (err) {
            log.error({ err }, 'Error handling fill event');
          }
        }
      },

      onOpenPositionUpdate: async (data: WsOpenPositionUpdate) => {
        if (data.pair !== config.pair) return;
        positionManager.updateFromWs(data);
      },

      onPositionClosed: async (data: WsPositionClosed) => {
        if (data.pair !== config.pair) return;
        log.warn({ pair: data.pair }, 'Position CLOSED — cancelling grid, entering cooldown');

        positionManager.handlePositionClosed(data);

        // Cancel all grid orders + TPSL
        try {
          await orderManager.cancelAll();
        } catch (err) {
          log.error({ err }, 'Error cancelling grid orders after position close — DB still cleared');
        }
        try {
          await tpslManager.cancelAll();
        } catch (err) {
          log.error({ err }, 'Error cancelling TPSL after position close');
        }

        // Reset grid state
        if (config.mode === 'neutral') {
          gridState = null;
        }

        // Enter cooldown
        riskManager.enterCooldown(store);
      },

      onFailedOrder: (data: WsFailedOrder) => {
        log.error({ data }, 'Order placement FAILED via WS');
      },

      onFailedCancelOrder: (data: WsFailedOrder) => {
        log.warn({ data }, 'Order cancellation FAILED via WS');
      },

      onConnected: () => {
        log.info('Account WebSocket connected and subscribed');
      },

      onDisconnected: () => {
        log.warn('Account WebSocket disconnected');
      },
    },
    config.wsStaleTimeoutSecs * 1000
  );

  accountWs.connect();

  // Wait for account WS to connect
  await new Promise<void>((resolve) => setTimeout(resolve, 2000));

  // ─── 8. Place initial grid + TPSL ─────────────────────────────────────────
  try {
    riskManager.checkCooldown(store);
  } catch (err) {
    log.warn({ err }, 'In cooldown — not placing grid yet');
  }

  const refPrice = await getReferencePrice(config, tradeWs, rest);
  log.info({ refPrice: refPrice.toString(), source: config.referencePriceSource }, 'Reference price');

  if (config.mode === 'neutral') {
    // Initialize pair-based grid state
    const seed = Date.now().toString(36);
    gridState = buildGridState(config, refPrice, constraints, availableBalance, seed);

    // Restore state from existing orders on exchange
    const activeOrders = orderManager.getActiveOrders();
    for (const order of activeOrders) {
      // Try to match to existing pair
      const level = order.level;
      const pair = gridState.pairs.find(p => p.levelIndex === level);
      if (pair) {
        if (order.side === 'BUY') {
          pair.bidLeg.state = 'active';
          pair.bidLeg.exchangeOrderId = order.exchangeOrderId || undefined;
        } else {
          pair.askLeg.state = 'active';
          pair.askLeg.exchangeOrderId = order.exchangeOrderId || undefined;
        }
      }
    }
    recalculateGridStats(gridState);
    logGridState(gridState, config);

    // Place missing legs
    if (!orderManager.isCircuitOpen()) {
      await placeMissingGridLegs(gridState, orderManager, config, constraints, availableBalance);
    }
  } else {
    // Legacy directional mode
    if (reconcileResult.needsGridPlacement && !orderManager.isCircuitOpen()) {
      try {
        riskManager.checkCooldown(store);
        
        if (reconcileResult.needsFullRebuild) {
          log.warn('Full rebuild requested — cancelling all existing grid orders');
          await orderManager.cancelAll();
          await tpslManager.cancelAll();
        }
        
        const seed = Date.now().toString(36);
        const allGridLevels = buildGridLevels(config, refPrice, constraints, seed, availableBalance);

        if (allGridLevels.length > 0) {
          log.info({ levels: allGridLevels.length, refPrice: refPrice.toString() }, 'Placing initial grid');
          await orderManager.placeGrid(allGridLevels);
        }
      } catch (err) {
        log.error({ err }, 'Failed to place initial grid');
      }
    }
  }

  if (reconcileResult.needsTpsl && positionManager.position) {
    try {
      await tpslManager.rebuild(positionManager.position, refPrice);
    } catch (err) {
      log.error({ err }, 'CRITICAL: Initial TPSL placement failed');
    }
  }

  // ─── 9. Periodic reconciliation ───────────────────────────────────────────
  setInterval(async () => {
    try {
      log.debug('Running periodic reconciliation');
      const positions = await rest.getOpenPositions(config.pair);
      positionManager.updateFromRest(positions);

      // Check for stale WS price
      if (tradeWs.isStale()) {
        log.warn('Trade WS price is stale — pausing grid operations');
        return;
      }

      // Ensure TPSL exists if position open
      if (!positionManager.isFlat && !store.getTpsl(config.pair)) {
        log.warn('TPSL missing during periodic check — rebuilding');
        const refPrice = await getReferencePrice(config, tradeWs, rest);
        await tpslManager.rebuild(positionManager.position!, refPrice);
      }

      store.set('last_reconcile', Date.now().toString());
    } catch (err) {
      log.error({ err }, 'Periodic reconciliation failed');
    }
  }, config.reconcileIntervalSecs * 1000);

  log.info({ pair: config.pair, mode: config.mode }, 'Grid bot running');

  // Keep alive
  process.on('SIGTERM', () => {
    log.info('SIGTERM received — shutting down');
    accountWs.stop();
    tradeWs.stop();
    process.exit(0);
  });

  process.on('SIGINT', () => {
    log.info('SIGINT received — shutting down');
    accountWs.stop();
    tradeWs.stop();
    process.exit(0);
  });

  process.on('uncaughtException', (err) => {
    log.error({ err }, 'Uncaught exception — failing closed');
    accountWs.stop();
    tradeWs.stop();
    process.exit(1);
  });
}

main().catch((err) => {
  console.error('Fatal startup error:', err);
  process.exit(1);
});
