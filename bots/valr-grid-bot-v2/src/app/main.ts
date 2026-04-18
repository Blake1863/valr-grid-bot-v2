/**
 * VALR Grid Bot v2 — Adaptive grid with fixed range.
 * 
 * Core behavior:
 * - Fixed price range: reference ± (gridRangePercent/2)
 * - N price levels per side
 * - Bids placed ONLY below current price
 * - Asks placed ONLY above current price
 * - Order count adapts to price position
 * - NO recenter — grid range is fixed forever
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
import {
  initGridState,
  updateGridOrders,
  markLegFilled,
  getLegsToPlace,
  getLegsToCancel,
  logGridState,
  calculateQuantityPerLevel,
  type GridState,
} from '../strategy/gridPairManager.js';
import type { WsOrderStatusUpdate, WsOpenPositionUpdate, WsPositionClosed, WsFailedOrder } from '../exchange/types.js';
import { createLogger } from './logger.js';

const log = createLogger('main');

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

  if (config.referencePriceSource === 'mark_price' && tradeWs.markPrice) {
    return tradeWs.markPrice;
  }
  if (config.referencePriceSource === 'mid_price' && tradeWs.midPrice) {
    return tradeWs.midPrice;
  }
  if (config.referencePriceSource === 'last_traded' && tradeWs.lastTradedPrice) {
    return tradeWs.lastTradedPrice;
  }

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

  throw new Error('Cannot determine reference price');
}

async function main(): Promise<void> {
  log.info('VALR Grid Bot v2 starting (adaptive grid with fixed range)');

  const config = loadConfig();

  if (config.dryRun) {
    log.warn('DRY-RUN MODE — no real orders');
  }

  log.info('Fetching API credentials');
  const apiKey = getSecret('valr_api_key');
  const apiSecret = getSecret('valr_api_secret');

  const rest = new ValrRestClient(apiKey, apiSecret, config.subaccountId);
  const store = new StateStore();
  const metadataLoader = new PairMetadataLoader(rest);
  const constraints = await metadataLoader.load(config.pair);

  const balances = await rest.getBalances();
  const usdtBalance = balances.find(b => b.currency === 'USDT' || b.currency === 'usdt');
  const availableBalance = usdtBalance ? new Decimal(usdtBalance.available) : new Decimal(0);
  log.info({ available: usdtBalance?.available ?? 'N/A' }, 'USDT balance');

  const positionManager = new PositionManager(config.pair, store);
  positionManager.loadFromStore();

  const riskManager = new RiskManager(config, constraints);
  const tpslManager = new TpslManager(rest, config, constraints, store);
  const orderManager = new OrderManager(rest, store, config, constraints);

  const tradeWs = new WsTradeClient(config.pair, config.wsStaleTimeoutSecs * 1000);
  tradeWs.connect();

  await new Promise<void>((resolve) => {
    const start = Date.now();
    const check = setInterval(() => {
      if (tradeWs.bestPrice || Date.now() - start > 5000) {
        clearInterval(check);
        resolve();
      }
    }, 100);
  });

  const refPrice = await getReferencePrice(config, tradeWs, rest);
  const currentPrice = tradeWs.bestPrice ?? refPrice;
  
  log.info(
    { refPrice: refPrice.toString(), currentPrice: currentPrice.toString() },
    'Reference and current price'
  );

  // Initialize grid state
  const seed = Date.now().toString(36);
  gridState = initGridState(config, refPrice, currentPrice, constraints, availableBalance, seed);
  logGridState(gridState, config);

  // Place initial orders
  if (!orderManager.isCircuitOpen()) {
    try {
      riskManager.checkCooldown(store);
      const toPlace = getLegsToPlace(gridState);
      log.info({ count: toPlace.length }, 'Placing initial grid orders');
      
      for (const leg of toPlace) {
        try {
          await orderManager.placeSingleOrder({
            level: leg.level.levelIndex,
            side: leg.side,
            price: leg.price,
            priceStr: leg.priceStr,
            quantity: leg.quantity,
            quantityStr: leg.quantityStr,
            customerOrderId: leg.customerOrderId,
          });
          // Mark as placed (exchangeOrderId will be set by WS)
          if (leg.side === 'BUY') {
            leg.level.bidLeg.exchangeOrderId = 'pending';
          } else {
            leg.level.askLeg.exchangeOrderId = 'pending';
          }
        } catch (err) {
          log.error({ err, level: leg.level.levelIndex, side: leg.side }, 'Failed to place order');
        }
      }
    } catch (err) {
      log.error({ err }, 'Failed to place initial grid');
    }
  }

  // Place TPSL if position exists
  if (positionManager.position) {
    try {
      await tpslManager.rebuild(positionManager.position, refPrice);
    } catch (err) {
      log.error({ err }, 'Initial TPSL placement failed');
    }
  }

  const accountWs = new WsAccountClient(
    apiKey,
    apiSecret,
    {
      onOrderStatusUpdate: async (data: WsOrderStatusUpdate) => {
        const result = orderManager.handleOrderStatusUpdate(data);

        if (result === 'filled' && gridState) {
          try {
            const filled = markLegFilled(gridState, data.customerOrderId, data.orderId);
            if (!filled) return;

            const positions = await rest.getOpenPositions(config.pair);
            positionManager.updateFromRest(positions);

            const currentPrice = await getReferencePrice(config, tradeWs, rest);
            
            try {
              riskManager.checkCooldown(store);
            } catch {
              log.info('In cooldown — skipping replenishment');
              return;
            }

            // Update grid orders based on new price
            updateGridOrders(gridState, currentPrice);
            
            // Place any new orders that should be active
            const toPlace = getLegsToPlace(gridState);
            if (toPlace.length > 0) {
              log.info({ count: toPlace.length }, 'Replenishing grid');
              for (const leg of toPlace) {
                try {
                  await orderManager.placeSingleOrder({
                    level: leg.level.levelIndex,
                    side: leg.side,
                    price: leg.price,
                    priceStr: leg.priceStr,
                    quantity: leg.quantity,
                    quantityStr: leg.quantityStr,
                    customerOrderId: leg.customerOrderId,
                  });
                } catch (err) {
                  log.error({ err, level: leg.level.levelIndex, side: leg.side }, 'Failed to replenish');
                }
              }
            }

            // Cancel orders that are no longer valid
            const toCancel = getLegsToCancel(gridState);
            for (const leg of toCancel) {
              try {
                await rest.cancelOrder(leg.exchangeOrderId, config.pair);
                log.info({ level: leg.level.levelIndex, side: leg.side }, 'Cancelled order');
              } catch (err) {
                log.warn({ err, level: leg.level.levelIndex }, 'Failed to cancel');
              }
            }

            // Update TPSL if we have a position
            if (!positionManager.isFlat) {
              try {
                await tpslManager.rebuild(positionManager.position!, currentPrice);
              } catch (err) {
                log.error({ err }, 'TPSL rebuild failed');
              }
            }

            logGridState(gridState, config);
          } catch (err) {
            log.error({ err }, 'Error handling fill');
          }
        }
      },

      onOpenPositionUpdate: async (data: WsOpenPositionUpdate) => {
        if (data.pair !== config.pair) return;
        positionManager.updateFromWs(data);
      },

      onPositionClosed: async (data: WsPositionClosed) => {
        if (data.pair !== config.pair) return;
        log.warn({ pair: data.pair }, 'Position CLOSED');

        positionManager.handlePositionClosed(data);

        try {
          await orderManager.cancelAll();
        } catch (err) {
          log.error({ err }, 'Error cancelling grid orders');
        }
        try {
          await tpslManager.cancelAll();
        } catch (err) {
          log.error({ err }, 'Error cancelling TPSL');
        }

        gridState = null;
        riskManager.enterCooldown(store);
      },

      onFailedOrder: (data: WsFailedOrder) => {
        log.error({ data }, 'Order failed');
      },

      onFailedCancelOrder: (data: WsFailedOrder) => {
        log.warn({ data }, 'Cancel failed');
      },

      onConnected: () => {
        log.info('Account WS connected');
      },

      onDisconnected: () => {
        log.warn('Account WS disconnected');
      },
    },
    config.wsStaleTimeoutSecs * 1000
  );

  accountWs.connect();
  await new Promise<void>((resolve) => setTimeout(resolve, 2000));

  // Periodic reconciliation
  setInterval(async () => {
    try {
      if (tradeWs.isStale()) {
        log.warn('Trade WS stale — pausing');
        return;
      }

      const positions = await rest.getOpenPositions(config.pair);
      positionManager.updateFromRest(positions);

      if (gridState) {
        const currentPrice = await getReferencePrice(config, tradeWs, rest);
        const oldBidCount = gridState.activeBidCount;
        const oldAskCount = gridState.activeAskCount;
        
        updateGridOrders(gridState, currentPrice);
        
        // Log if order counts changed
        if (gridState.activeBidCount !== oldBidCount || gridState.activeAskCount !== oldAskCount) {
          logGridState(gridState, config);
        }

        // Place missing orders
        const toPlace = getLegsToPlace(gridState);
        for (const leg of toPlace) {
          try {
            await orderManager.placeSingleOrder({
              level: leg.level.levelIndex,
              side: leg.side,
              price: leg.price,
              priceStr: leg.priceStr,
              quantity: leg.quantity,
              quantityStr: leg.quantityStr,
              customerOrderId: leg.customerOrderId,
            });
          } catch (err) {
            log.warn({ err, level: leg.level.levelIndex }, 'Failed to place');
          }
        }

        // Cancel invalid orders
        const toCancel = getLegsToCancel(gridState);
        for (const leg of toCancel) {
          try {
            await rest.cancelOrder(leg.exchangeOrderId, config.pair);
          } catch (err) {
            log.warn({ err, level: leg.level.levelIndex }, 'Failed to cancel');
          }
        }
      }

      if (!positionManager.isFlat && !store.getTpsl(config.pair)) {
        log.warn('TPSL missing — rebuilding');
        const refPrice = await getReferencePrice(config, tradeWs, rest);
        await tpslManager.rebuild(positionManager.position!, refPrice);
      }

      store.set('last_reconcile', Date.now().toString());
    } catch (err) {
      log.error({ err }, 'Periodic reconciliation failed');
    }
  }, config.reconcileIntervalSecs * 1000);

  log.info({ pair: config.pair, range: config.gridRangePercent + '%' }, 'Grid bot running');

  process.on('SIGTERM', () => {
    log.info('Shutting down');
    accountWs.stop();
    tradeWs.stop();
    process.exit(0);
  });

  process.on('SIGINT', () => {
    log.info('Shutting down');
    accountWs.stop();
    tradeWs.stop();
    process.exit(0);
  });

  process.on('uncaughtException', (err) => {
    log.error({ err }, 'Uncaught exception');
    accountWs.stop();
    tradeWs.stop();
    process.exit(1);
  });
}

main().catch((err) => {
  console.error('Fatal:', err);
  process.exit(1);
});
