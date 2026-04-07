/**
 * VALR Grid Bot v2 — Main entry point
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
import { buildGridLevels, buildReplenishLevel, calcSlPrice, getSpacingAmount, calcTpPrice } from '../strategy/gridBuilder.js';
import type { WsOrderStatusUpdate, WsOpenPositionUpdate, WsPositionClosed, WsFailedOrder } from '../exchange/types.js';
import { createLogger } from './logger.js';

const log = createLogger('main');

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

async function main(): Promise<void> {
  log.info('VALR Grid Bot v2 starting');

  // ─── 1. Load config ───────────────────────────────────────────────────────
  const config = loadConfig();

  if (config.dryRun) {
    log.warn('DRY-RUN MODE — no real orders will be placed');
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
        const filledOrder = orderManager.getFilledOrderData(data);
        const result = orderManager.handleOrderStatusUpdate(data);

        if (result === 'filled') {
          try {
            // Refresh position from exchange (authoritative)
            const positions = await rest.getOpenPositions(config.pair);
            positionManager.updateFromRest(positions);

            const refPrice = await getReferencePrice(config, tradeWs, rest);
            const seed = Date.now().toString(36);

            if (config.mode === 'neutral') {
              // ── NEUTRAL MODE ─────────────────────────────────────────────
              // Replenish the side that just filled with a new deeper order.
              // The existing orders on the opposite side ARE the take profits.
              if (filledOrder) {
                const filledSide = filledOrder.side as 'BUY' | 'SELL';
                const activeOrders = orderManager.getActiveOrders();
                const activeSameSide = activeOrders.filter((o) => o.side === filledSide);
                const nextLevel = activeSameSide.length + 1;

                // Only replenish if within max position limit
                const currentQty = positionManager.netQuantity;
                const maxNet = new Decimal(config.maxNetPosition);
                const qtyPerLevel = new Decimal(config.quantityPerLevel);

                if (currentQty.plus(qtyPerLevel).lte(maxNet)) {
                  const replenish = buildReplenishLevel(
                    config, refPrice, constraints, filledSide, nextLevel, seed
                  );
                  log.info(
                    { side: filledSide, level: nextLevel, price: replenish.priceStr },
                    'Neutral: replenishing filled side'
                  );
                  try {
                    await orderManager.placeSingleOrder(replenish);
                  } catch (err) {
                    log.warn({ err }, 'Failed to replenish after fill');
                  }
                } else {
                  log.warn(
                    { currentQty: currentQty.toString(), maxNet: maxNet.toString() },
                    'Neutral: max position reached — not replenishing'
                  );
                }
              }

              // Place SL if we have a net position (no TP — grid is its own TP)
              if (!positionManager.isFlat) {
                try {
                  await tpslManager.rebuild(positionManager.position!, refPrice);
                } catch (err) {
                  log.error({ err }, 'CRITICAL: SL placement failed after fill');
                }
              }

            } else {
              // ── DIRECTIONAL MODES (long_only / short_only) ───────────────
              if (!positionManager.isFlat) {
                try {
                  await tpslManager.rebuild(positionManager.position!, refPrice);
                } catch (err) {
                  log.error({ err }, 'CRITICAL: TPSL placement failed after fill — halting new entries');
                  return;
                }

                // Replenish next deeper level
                const activeOrders = orderManager.getActiveOrders();
                const currentQty = positionManager.netQuantity;
                const maxNet = new Decimal(config.maxNetPosition);
                const qtyPerLevel = new Decimal(config.quantityPerLevel);

                if (activeOrders.length < config.levels && currentQty.plus(qtyPerLevel).lte(maxNet)) {
                  const nextLevel = activeOrders.length + 1;
                  const newLevels = buildGridLevels(config, refPrice, constraints, seed);
                  const nextLevelData = newLevels.find((l) => l.level === nextLevel);
                  if (nextLevelData) {
                    try {
                      await orderManager.placeSingleOrder(nextLevelData);
                    } catch (err) {
                      log.warn({ err }, 'Failed to replenish grid level after fill');
                    }
                  }
                }
              }
            }
          } catch (err) {
            log.error({ err }, 'Error handling fill event');
          }
        }
      },

      onOpenPositionUpdate: async (data: WsOpenPositionUpdate) => {
        if (data.pair !== config.pair) return;
        positionManager.updateFromWs(data);

        if (!positionManager.isFlat) {
          try {
            const refPrice = await getReferencePrice(config, tradeWs, rest);
            await tpslManager.rebuild(positionManager.position!, refPrice);
          } catch (err) {
            log.error({ err }, 'CRITICAL: TPSL rebuild failed after position update');
          }
        }
      },

      onPositionClosed: async (data: WsPositionClosed) => {
        if (data.pair !== config.pair) return;
        log.warn({ pair: data.pair }, 'Position CLOSED — cancelling grid, entering cooldown');

        positionManager.handlePositionClosed(data);

        // Cancel all grid orders + TPSL
        try {
          await orderManager.cancelAll();
          await tpslManager.cancelAll();
        } catch (err) {
          log.error({ err }, 'Error cancelling orders after position close');
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
    log.info('Waiting for cooldown to expire...');
  }

  const refPrice = await getReferencePrice(config, tradeWs, rest);
  log.info({ refPrice: refPrice.toString(), source: config.referencePriceSource }, 'Reference price');

  if (reconcileResult.needsGridPlacement && !orderManager.isCircuitOpen()) {
    try {
      riskManager.checkCooldown(store);
      const seed = Date.now().toString(36);
      const allGridLevels = buildGridLevels(config, refPrice, constraints, seed);

      // In neutral mode, only place the missing side (avoid duplicating existing orders)
      let gridLevels = allGridLevels;
      if (config.mode === 'neutral') {
        const activeOrders = orderManager.getActiveOrders();
        const activeBuys = activeOrders.filter((o) => o.side === 'BUY').length;
        const activeSells = activeOrders.filter((o) => o.side === 'SELL').length;
        gridLevels = allGridLevels.filter((l) => {
          if (l.side === 'BUY' && activeBuys >= config.levels) return false;
          if (l.side === 'SELL' && activeSells >= config.levels) return false;
          return true;
        });
      }

      if (gridLevels.length > 0) {
        log.info({ levels: gridLevels.length, refPrice: refPrice.toString() }, 'Placing initial grid');
        for (const level of gridLevels) {
          log.info({ level: level.level, side: level.side, price: level.priceStr, qty: level.quantityStr });
        }
        await orderManager.placeGrid(gridLevels);
      } else {
        log.info('Grid already fully populated on exchange — no placement needed');
      }
    } catch (err) {
      log.error({ err }, 'Failed to place initial grid');
    }
  }

  if (reconcileResult.needsTpsl && positionManager.position) {
    try {
      await tpslManager.rebuild(positionManager.position, refPrice);
    } catch (err) {
      log.error({ err }, 'CRITICAL: Initial TPSL placement failed');
      // Don't exit — let the periodic reconcile handle it
    }
  }

  // ─── 9. Periodic reconciliation ───────────────────────────────────────────
  setInterval(async () => {
    try {
      log.debug('Running periodic reconciliation');
      const positions = await rest.getOpenPositions(config.pair);
      positionManager.updateFromRest(positions);

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
