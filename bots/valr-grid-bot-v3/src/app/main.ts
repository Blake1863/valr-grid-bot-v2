/**
 * VALR Grid Bot v3 — OKX/Bybit Style Neutral Futures Grid
 * 
 * Main entry point.
 */

import Decimal from 'decimal.js';
import { createLogger } from './logger.js';
import { loadConfig } from '../config/loader.js';
import type { BotConfig } from '../config/schema.js';
import { ValrRestClient } from '../exchange/restClient.js';
import { WSPriceClient, type PriceUpdate } from '../exchange/wsPriceClient.js';
import { WSAccountClient, type AccountUpdate } from '../exchange/wsAccountClient.js';
import { getPairConstraints, validateOrder } from '../exchange/pairMetadata.js';
import { buildGrid, assignNeutralSides, calculateQuantityPerLevel } from '../strategy/gridBuilder.js';
import { initGridState, updateGridState, handleOrderFill, markOrderActive, markOrderCancelled, setDataHealthy } from '../strategy/gridManager.js';
import { createStore, type StoredOrder, type StoredCycle } from '../state/store.js';

const log = createLogger('main');

// Bot instance state
let config: BotConfig;
let restClient: ValrRestClient;
let priceClient: WSPriceClient;
let accountClient: WSAccountClient;
let store: any;
let gridState: any;
let reconciliationInterval: NodeJS.Timeout | null = null;

/**
 * Initialize and start the bot.
 */
async function main(): Promise<void> {
  log.info('Starting VALR Grid Bot v3 (OKX/Bybit Style)');

  // Load config
  config = loadConfig();
  log.info({ pair: config.pair, mode: config.mode }, 'Loaded configuration');

  // Initialize REST client
  const apiKey = process.env.VALR_API_KEY!;
  const apiSecret = process.env.VALR_API_SECRET!;
  
  restClient = new ValrRestClient({
    apiKey,
    apiSecret,
    subaccountId: config.subaccountId,
    dryRun: config.dryRun,
  });

  // Initialize state store
  store = createStore(config.pair.toLowerCase());
  log.info('State store initialized');

  // Get pair constraints
  const constraints = getPairConstraints(config.pair);
  log.info({ tickSize: constraints.tickSize.toString() }, 'Pair constraints');

  // Get initial price
  const ticker = await restClient.getTicker(config.pair);
  const currentPrice = new Decimal(ticker.last);
  const referencePrice = config.referencePrice 
    ? new Decimal(config.referencePrice)
    : currentPrice;

  log.info({ currentPrice: currentPrice.toString(), referencePrice: referencePrice.toString() }, 'Price data');

  // Build grid
  const construction = buildGrid(config, constraints);
  log.info({ levels: construction.levels.length, mode: config.gridMode }, 'Grid constructed');

  // Calculate quantity
  const balance = await restClient.getBalance('USDT');
  const availableBalance = balance ? new Decimal(balance.available) : new Decimal('100');
  const quantity = config.dynamicSizing
    ? calculateQuantityPerLevel(config, availableBalance, referencePrice, constraints)
    : new Decimal(config.quantityPerLevel!);

  log.info({ quantity: quantity.toString(), dynamic: config.dynamicSizing }, 'Quantity per level');

  // Assign sides (neutral mode)
  assignNeutralSides(construction.levels, referencePrice, Date.now().toString());

  // Initialize grid state
  gridState = initGridState(
    config,
    construction,
    referencePrice,
    currentPrice,
    constraints,
    quantity,
    Date.now().toString()
  );

  log.info({ inRange: gridState.inRange }, 'Grid state initialized');

  // Initialize WebSocket clients
  priceClient = new WSPriceClient({
    pairs: [config.pair],
    onPrice: handlePriceUpdate,
    staleTimeoutMs: config.staleDataTimeoutMs,
  });

  accountClient = new WSAccountClient({
    apiKey,
    apiSecret,
    subaccountId: config.subaccountId,
    onUpdate: handleAccountUpdate,
  });

  // Connect WebSockets
  priceClient.connect();
  accountClient.connect();

  // Wait for WS to be healthy
  await sleep(2000);

  // Initial reconciliation
  await reconcile();

  // Start reconciliation interval
  reconciliationInterval = setInterval(
    reconcile,
    config.reconciliationIntervalSecs * 1000
  );

  log.info('Bot started successfully');
}

/**
 * Handle price updates from WebSocket.
 */
function handlePriceUpdate(update: PriceUpdate): void {
  if (update.pair !== config.pair) return;

  const constraints = getPairConstraints(config.pair);
  const { inRange, toPlace, toCancel } = updateGridState(gridState, config, update.markPrice);

  setDataHealthy(gridState, priceClient.isHealthy());

  // Cancel orders that should no longer be active
  for (const order of toCancel) {
    if (order.exchangeOrderId) {
      restClient.cancelOrder(order.exchangeOrderId, config.pair)
        .then(() => {
          markOrderCancelled(gridState, order.customerOrderId);
          log.info({ level: order.levelIndex }, 'Cancelled order');
        })
        .catch(err => log.warn({ err }, 'Failed to cancel'));
    }
  }

  // Place new orders
  for (const order of toPlace) {
    placeOrder(order, constraints);
  }
}

/**
 * Handle account updates (order fills).
 */
function handleAccountUpdate(update: AccountUpdate): void {
  if (update.type !== 'order_update') return;

  const data = update.data;
  if (data.status === 'FILLED' || data.status === 'PARTIALLY_FILLED') {
    const customerOrderId = data.customerOrderId;
    const order = Array.from(gridState.orders.values()).find(o => o.customerOrderId === customerOrderId);
    
    if (order) {
      const constraints = getPairConstraints(config.pair);
      const fillPrice = new Decimal(data.averagePrice || data.price);
      const fillQty = new Decimal(data.filledQuantity);

      const result = handleOrderFill(gridState, config, order, fillPrice, fillQty, constraints);
      
      if (result.completionOrder) {
        // Place completion order
        placeOrder(result.completionOrder, constraints);
      }
      
      if (result.cycle) {
        log.info({ profit: result.cycle.realizedProfit.toString() }, 'Cycle completed');
        saveCycle(result.cycle);
      }
    }
  }
}

/**
 * Place an order on the exchange.
 */
async function placeOrder(order: any, constraints: any): Promise<void> {
  // Validate order
  const validation = validateOrder(order.price, order.quantity, constraints);
  if (!validation.valid) {
    log.warn({ error: validation.error }, 'Order validation failed');
    return;
  }

  try {
    const result = await restClient.placeLimitOrder({
      pair: config.pair,
      side: order.side,
      type: 'LIMIT',
      price: order.priceStr,
      quantity: order.quantityStr,
      customerOrderId: order.customerOrderId,
      postOnly: config.postOnly,
      subaccountId: config.subaccountId,
    });

    markOrderActive(gridState, order.customerOrderId, result.orderId);
    order.state = 'active';
    order.exchangeOrderId = result.orderId;

    // Persist to store
    saveOrder(order);

    log.info({ level: order.levelIndex, side: order.side, price: order.priceStr }, 'Order placed');
  } catch (err: any) {
    log.error({ err: err.message, level: order.levelIndex }, 'Failed to place order');
  }
}

/**
 * Reconcile bot state with exchange.
 */
async function reconcile(): Promise<void> {
  log.debug('Reconciliation started');

  try {
    // Fetch open orders
    const openOrders = await restClient.getOpenOrders(config.pair);
    
    // Map exchange orders to grid orders
    for (const exOrder of openOrders) {
      const gridOrder = Array.from(gridState.orders.values()).find(
        o => o.customerOrderId === exOrder.customerOrderId
      );
      
      if (gridOrder && !gridOrder.exchangeOrderId) {
        gridOrder.exchangeOrderId = exOrder.orderId;
        gridOrder.state = 'active';
        markOrderActive(gridState, gridOrder.customerOrderId, exOrder.orderId);
      }
    }

    // Check for missing orders
    for (const [levelIdx, order] of gridState.orders) {
      if (order.state === 'active' && !order.exchangeOrderId) {
        // Order marked active but no exchange ID — reconcile
        const exOrder = openOrders.find(o => o.customerOrderId === order.customerOrderId);
        if (exOrder) {
          order.exchangeOrderId = exOrder.orderId;
        } else {
          order.state = 'missing';
        }
      }
    }

    log.debug('Reconciliation complete');
  } catch (err: any) {
    log.error({ err: err.message }, 'Reconciliation failed');
  }
}

/**
 * Save order to persistent store.
 */
function saveOrder(order: any): void {
  const stored: StoredOrder = {
    levelIndex: order.levelIndex,
    customerOrderId: order.customerOrderId,
    exchangeOrderId: order.exchangeOrderId,
    side: order.side,
    price: order.priceStr,
    quantity: order.quantityStr,
    role: order.role,
    state: order.state,
    createdAt: new Date().toISOString(),
  };
  store.saveOrder(stored);
}

/**
 * Save cycle to persistent store.
 */
function saveCycle(cycle: any): void {
  const stored: StoredCycle = {
    cycleId: cycle.cycleId,
    entryLevelIndex: cycle.entryLevelIndex,
    exitLevelIndex: cycle.exitLevelIndex,
    entrySide: cycle.entrySide,
    entryPrice: cycle.entryPrice.toString(),
    exitPrice: cycle.exitPrice.toString(),
    quantity: cycle.quantity.toString(),
    realizedProfit: cycle.realizedProfit.toString(),
    completedAt: cycle.completedAt,
  };
  store.saveCycle(stored);
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// Start the bot
main().catch(err => {
  log.error({ err: err.message }, 'Bot failed to start');
  process.exit(1);
});
