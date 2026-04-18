/**
 * VALR Grid Bot v2 — Bounded Price-Range Grid
 * 
 * Core behavior:
 * - Fixed price band: [lowerBound, upperBound]
 * - Exactly N live resting entry orders (total)
 * - Bid/ask split varies naturally with price position
 * - Deterministic level selection (nearest to reference)
 * - Never place orders outside configured range
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
  updateGridState,
  markLevelFilled,
  markLevelActive,
  getLevelsToPlace,
  getLevelsToCancel,
  logGridState,
  type GridState,
} from '../strategy/gridManager.js';
import type { WsOrderStatusUpdate, WsOpenPositionUpdate, WsPositionClosed, WsFailedOrder } from '../exchange/types.js';
import { createLogger } from './logger.js';

const log = createLogger('main');

let gridState: GridState | null = null;
let isDataStale = false;

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
  log.info('VALR Grid Bot v2 starting (bounded price-range grid)');

  const config = loadConfig();

  if (config.dryRun) {
    log.warn('DRY-RUN MODE — no real orders');
  }

  // Validate bounds
  const lowerBound = new Decimal(config.lowerBound);
  const upperBound = new Decimal(config.upperBound);
  if (lowerBound.gte(upperBound)) {
    throw new Error(`Invalid bounds: lowerBound (${lowerBound}) must be < upperBound (${upperBound})`);
  }
  log.info({ lowerBound: lowerBound.toString(), upperBound: upperBound.toString(), levels: config.levels }, 'Grid configuration');

  log.info('Fetching API credentials');
  const apiKey = getSecret('valr_grid_bot_1_api_key');
  const apiSecret = getSecret('valr_grid_bot_1_api_secret');
  log.info({ apiKey: apiKey.substring(0, 16) + '...', hasSecret: !!apiSecret }, 'Credentials loaded');

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

  // Wait for initial price (up to 5s)
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

  // Check if price is outside bounds
  if (currentPrice.lt(lowerBound)) {
    log.warn({ currentPrice: currentPrice.toString(), lowerBound: lowerBound.toString() }, 
      'Price is below lower bound — will place asks only when price enters range');
  } else if (currentPrice.gt(upperBound)) {
    log.warn({ currentPrice: currentPrice.toString(), upperBound: upperBound.toString() },
      'Price is above upper bound — will place bids only when price enters range');
  }

  // Initialize grid state
  const seed = Date.now().toString(36);
  gridState = initGridState(config, lowerBound, upperBound, refPrice, currentPrice, constraints, availableBalance, seed);
  logGridState(gridState, config);

  // Place initial orders
  if (!orderManager.isCircuitOpen()) {
    try {
      riskManager.checkCooldown(store);
      const toPlace = getLevelsToPlace(gridState);
      log.info({ count: toPlace.length }, 'Placing initial grid orders');
      
      for (const level of toPlace) {
        try {
          await orderManager.placeSingleOrder(level);
          level.state = 'pending';
        } catch (err) {
          log.error({ err, level: level.levelIndex, side: level.side }, 'Failed to place order');
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
            const filled = markLevelFilled(gridState, data.customerOrderId, data.orderId);
            if (!filled) return;

            // Update position from exchange
            const positions = await rest.getOpenPositions(config.pair);
            positionManager.updateFromRest(positions);

            const currentPrice = await getReferencePrice(config, tradeWs, rest);
            
            try {
              riskManager.checkCooldown(store);
            } catch {
              log.info('In cooldown — skipping replenishment');
              return;
            }

            // Recompute active levels and replenish
            const { toPlace, toCancel, bidCount, askCount } = updateGridState(gridState, config, currentPrice);
            
            log.info({ bidCount, askCount, total: bidCount + askCount, target: config.levels }, 
              'Active level count after fill');

            // Place missing orders
            if (toPlace.length > 0) {
              log.info({ count: toPlace.length }, 'Replenishing grid levels');
              for (const level of toPlace) {
                try {
                  await orderManager.placeSingleOrder(level);
                } catch (err) {
                  log.error({ err, level: level.levelIndex, side: level.side }, 'Failed to replenish');
                }
              }
            }

            // Cancel orders no longer in active set
            if (toCancel.length > 0) {
              log.info({ count: toCancel.length }, 'Cancelling stale orders');
              for (const level of toCancel) {
                try {
                  await rest.cancelOrder(level.exchangeOrderId!, config.pair);
                  level.state = 'cancelled';
                  level.exchangeOrderId = undefined;
                } catch (err) {
                  log.warn({ err, level: level.levelIndex }, 'Failed to cancel');
                }
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
        } else if (result === 'cancelled' && gridState) {
          // Mark as cancelled and trigger replenishment
          markLevelActive(gridState, data.customerOrderId || '', data.orderId);
          const { toPlace } = updateGridState(gridState, config, gridState.currentPrice);
          if (toPlace.length > 0) {
            log.info({ count: toPlace.length }, 'Replenishing after cancellation');
            for (const level of toPlace) {
              try {
                await orderManager.placeSingleOrder(level);
              } catch (err) {
                log.error({ err, level: level.levelIndex }, 'Failed to replenish');
              }
            }
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
      // Check for stale data
      if (tradeWs.isStale()) {
        if (!isDataStale) {
          log.warn('Trade WS stale — pausing new entry placement');
          isDataStale = true;
        }
        return;
      }
      if (isDataStale) {
        log.info('Trade WS data fresh — resuming operations');
        isDataStale = false;
      }

      const positions = await rest.getOpenPositions(config.pair);
      positionManager.updateFromRest(positions);

      if (gridState && !isDataStale) {
        const currentPrice = await getReferencePrice(config, tradeWs, rest);
        const { toPlace, toCancel, bidCount, askCount } = updateGridState(gridState, config, currentPrice);
        
        // Place missing orders
        for (const level of toPlace) {
          if (!level.exchangeOrderId) {
            try {
              await orderManager.placeSingleOrder(level);
            } catch (err) {
              log.warn({ err, level: level.levelIndex }, 'Failed to place');
            }
          }
        }

        // Cancel invalid orders
        for (const level of toCancel) {
          try {
            await rest.cancelOrder(level.exchangeOrderId!, config.pair);
            level.state = 'cancelled';
            level.exchangeOrderId = undefined;
          } catch (err) {
            log.warn({ err, level: level.levelIndex }, 'Failed to cancel');
          }
        }

        // Log if counts changed significantly
        const totalActive = bidCount + askCount;
        if (totalActive !== config.levels) {
          log.info({ active: totalActive, target: config.levels, bidCount, askCount }, 
            'Active order count differs from target — reconciliation in progress');
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

  log.info(
    { 
      pair: config.pair, 
      levels: config.levels,
      range: `${lowerBound.toString()} – ${upperBound.toString()}`
    }, 
    'Grid bot running'
  );

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
