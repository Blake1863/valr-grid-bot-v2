import { describe, it, expect, vi, beforeEach } from 'vitest';
import { reconcile } from '../src/strategy/reconciliation.js';
import type { BotConfig } from '../src/config/schema.js';
import { PositionManager } from '../src/strategy/positionManager.js';
import type { ValrOpenPosition, ValrOpenOrder } from '../src/exchange/types.js';

const config: BotConfig = {
  pair: 'SOLUSDTPERP',
  subaccountId: '',
  mode: 'long_only',
  levels: 3,
  spacingMode: 'percent',
  spacingValue: '0.4',
  quantityPerLevel: '1.5',
  maxNetPosition: '4.5',
  stopLossMode: 'percent',
  stopLossValue: '3.0',
  tpMode: 'one_level',
  triggerType: 'MARK_PRICE',
  referencePriceSource: 'mark_price',
  leverage: 5,
  postOnly: true,
  allowMargin: true,
  cooldownAfterStopSecs: 300,
  dryRun: false,
  reconcileIntervalSecs: 60,
  maxActiveGridOrders: 10,
  wsStaleTimeoutSecs: 30,
};

function makeStore(activeOrders: unknown[] = [], tpsl?: unknown) {
  return {
    getActiveGridOrders: vi.fn(() => activeOrders),
    getTpsl: vi.fn(() => tpsl),
    upsertGridOrder: vi.fn(),
    updateGridOrderStatus: vi.fn(),
    getPosition: vi.fn(() => undefined),
    clearPosition: vi.fn(),
    upsertPosition: vi.fn(),
  } as unknown as ConstructorParameters<typeof reconcile>[1];
}

function makeRest(positions: ValrOpenPosition[] = [], openOrders: ValrOpenOrder[] = []) {
  return {
    getOpenPositions: vi.fn(async () => positions),
    getOpenOrders: vi.fn(async () => openOrders),
  } as unknown as ConstructorParameters<typeof reconcile>[0];
}

describe('reconciliation', () => {
  it('exchange flat + local flat → needsGridPlacement true, hasPosition false', async () => {
    const rest = makeRest([], []);
    const store = makeStore([]);
    const pm = new PositionManager(config.pair, {
      getPosition: () => undefined,
      clearPosition: () => {},
      upsertPosition: () => {},
    } as any);

    const result = await reconcile(rest, store, pm, config);

    expect(result.hasPosition).toBe(false);
    expect(result.needsGridPlacement).toBe(true);
    expect(result.needsTpsl).toBe(false);
  });

  it('exchange has position + local flat → hasPosition true, rebuild from exchange', async () => {
    const position: ValrOpenPosition = {
      pair: 'SOLUSDTPERP',
      side: 'buy',
      quantity: '3',
      realisedPnl: '0',
      totalSessionEntryQuantity: '3',
      totalSessionValue: '360',
      sessionAverageEntryPrice: '120',
      averageEntryPrice: '119.50',
      unrealisedPnl: '1.5',
      updatedAt: '2025-01-01T00:00:00Z',
      createdAt: '2025-01-01T00:00:00Z',
      positionId: 'pos-1',
    };

    const rest = makeRest([position], []);
    const store = makeStore([]);
    const pm = new PositionManager(config.pair, {
      getPosition: () => undefined,
      clearPosition: () => {},
      upsertPosition: () => {},
    } as any);

    const result = await reconcile(rest, store, pm, config);

    expect(result.hasPosition).toBe(true);
    expect(result.needsTpsl).toBe(true);
    expect(pm.isFlat).toBe(false);
    expect(pm.position!.averageEntryPrice.toString()).toBe('119.5');
  });

  it('orphaned exchange orders are identified', async () => {
    const orphanOrder: ValrOpenOrder = {
      orderId: 'orphan-uuid',
      side: 'BUY',
      remainingQuantity: '1',
      price: '119',
      currencyPair: 'SOLUSDTPERP',
      createdAt: '2025-01-01T00:00:00Z',
      type: 'LIMIT_POST_ONLY',
      customerOrderId: 'unknown-external-order',
    };

    const rest = makeRest([], [orphanOrder]);
    const store = makeStore([]);
    const pm = new PositionManager(config.pair, {
      getPosition: () => undefined,
      clearPosition: () => {},
      upsertPosition: () => {},
    } as any);

    const result = await reconcile(rest, store, pm, config);
    expect(result.orphanedOrderIds).toContain('orphan-uuid');
  });

  it('grid orders from previous run are restored', async () => {
    const previousRunOrder: ValrOpenOrder = {
      orderId: 'prev-run-uuid',
      side: 'BUY',
      remainingQuantity: '1.5',
      price: '119.52',
      currencyPair: 'SOLUSDTPERP',
      createdAt: '2025-01-01T00:00:00Z',
      type: 'LIMIT_POST_ONLY',
      customerOrderId: 'grid-L-1-abc123', // matches our naming convention
    };

    const rest = makeRest([], [previousRunOrder]);
    const store = makeStore([]);
    const pm = new PositionManager(config.pair, {
      getPosition: () => undefined,
      clearPosition: () => {},
      upsertPosition: () => {},
    } as any);

    const result = await reconcile(rest, store, pm, config);
    // Should be restored, not orphaned
    expect(result.orphanedOrderIds).not.toContain('prev-run-uuid');
  });
});
