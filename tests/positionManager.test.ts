import { describe, it, expect, beforeEach } from 'vitest';
import { PositionManager } from '../src/strategy/positionManager.js';
import type { ValrOpenPosition, WsOpenPositionUpdate, WsPositionClosed } from '../src/exchange/types.js';

// Mock store
function mockStore() {
  const data: Record<string, unknown> = {};
  return {
    upsertPosition: (pos: unknown) => { data['position'] = pos; },
    getPosition: () => data['position'] as ReturnType<typeof mockStore>['getPosition'] | undefined,
    clearPosition: () => { delete data['position']; },
  } as unknown as ConstructorParameters<typeof PositionManager>[1];
}

describe('PositionManager', () => {
  let pm: PositionManager;
  const pair = 'SOLUSDTPERP';

  beforeEach(() => {
    pm = new PositionManager(pair, mockStore());
  });

  it('starts flat', () => {
    expect(pm.isFlat).toBe(true);
    expect(pm.position).toBeNull();
  });

  it('updateFromRest with empty array → stays flat', () => {
    pm.updateFromRest([]);
    expect(pm.isFlat).toBe(true);
  });

  it('updateFromRest with position → sets state correctly', () => {
    const pos: ValrOpenPosition = {
      pair,
      side: 'buy',
      quantity: '2.5',
      realisedPnl: '0',
      totalSessionEntryQuantity: '2.5',
      totalSessionValue: '300',
      sessionAverageEntryPrice: '120',
      averageEntryPrice: '118.50',
      unrealisedPnl: '3.75',
      updatedAt: '2025-01-01T00:00:00Z',
      createdAt: '2025-01-01T00:00:00Z',
      positionId: 'pos-uuid-1',
    };

    pm.updateFromRest([pos]);
    expect(pm.isFlat).toBe(false);
    expect(pm.position!.quantity.toString()).toBe('2.5');
    expect(pm.position!.averageEntryPrice.toString()).toBe('118.5');
    expect(pm.position!.positionId).toBe('pos-uuid-1');
  });

  it('averageEntryPrice is used from exchange field (not sessionAverage)', () => {
    const pos: ValrOpenPosition = {
      pair,
      side: 'buy',
      quantity: '1',
      realisedPnl: '0',
      totalSessionEntryQuantity: '1',
      totalSessionValue: '100',
      sessionAverageEntryPrice: '999',   // different
      averageEntryPrice: '120.00',        // THIS is the one we use
      unrealisedPnl: '0',
      updatedAt: '2025-01-01T00:00:00Z',
      createdAt: '2025-01-01T00:00:00Z',
      positionId: 'pos-1',
    };
    pm.updateFromRest([pos]);
    expect(pm.position!.averageEntryPrice.toString()).toBe('120');
  });

  it('updateFromWs updates position state', () => {
    const data: WsOpenPositionUpdate = {
      pair,
      side: 'buy',
      quantity: '3',
      realisedPnl: '0.5',
      totalSessionEntryQuantity: '3',
      totalSessionValue: '360',
      sessionAverageEntryPrice: '120',
      averageEntryPrice: '119.50', // SOURCE OF TRUTH
      unrealisedPnl: '1.5',
      updatedAt: '2025-01-01T01:00:00Z',
      createdAt: '2025-01-01T00:00:00Z',
      positionId: 'pos-uuid-2',
      leverageTier: 5,
    };
    pm.updateFromWs(data);
    expect(pm.isFlat).toBe(false);
    expect(pm.position!.averageEntryPrice.toString()).toBe('119.5');
    expect(pm.position!.quantity.toString()).toBe('3');
  });

  it('POSITION_CLOSED clears state', () => {
    // Set up a position first
    pm.updateFromRest([{
      pair,
      side: 'buy',
      quantity: '2',
      realisedPnl: '0',
      totalSessionEntryQuantity: '2',
      totalSessionValue: '240',
      sessionAverageEntryPrice: '120',
      averageEntryPrice: '120',
      unrealisedPnl: '0',
      updatedAt: '2025-01-01T00:00:00Z',
      createdAt: '2025-01-01T00:00:00Z',
      positionId: 'pos-1',
    }]);
    expect(pm.isFlat).toBe(false);

    const closed: WsPositionClosed = { pair, positionId: 'pos-1' };
    pm.handlePositionClosed(closed);

    expect(pm.isFlat).toBe(true);
    expect(pm.position).toBeNull();
  });

  it('exchange position exists but local flat → rebuild from exchange', () => {
    // Simulate: bot restarts, local is flat (new PositionManager)
    const freshPm = new PositionManager(pair, mockStore());
    expect(freshPm.isFlat).toBe(true);

    // Exchange reports a position
    freshPm.updateFromRest([{
      pair,
      side: 'sell',
      quantity: '5',
      realisedPnl: '0',
      totalSessionEntryQuantity: '5',
      totalSessionValue: '600',
      sessionAverageEntryPrice: '120',
      averageEntryPrice: '121',
      unrealisedPnl: '-5',
      updatedAt: '2025-01-01T00:00:00Z',
      createdAt: '2025-01-01T00:00:00Z',
      positionId: 'pos-orphan',
    }]);

    expect(freshPm.isFlat).toBe(false);
    expect(freshPm.position!.side).toBe('sell');
    expect(freshPm.position!.quantity.toString()).toBe('5');
    expect(freshPm.position!.averageEntryPrice.toString()).toBe('121');
  });

  it('ignores position update for different pair', () => {
    const data: WsOpenPositionUpdate = {
      pair: 'BTCUSDTPERP', // different pair
      side: 'buy',
      quantity: '1',
      realisedPnl: '0',
      totalSessionEntryQuantity: '1',
      totalSessionValue: '100',
      sessionAverageEntryPrice: '100000',
      averageEntryPrice: '100000',
      unrealisedPnl: '0',
      updatedAt: '2025-01-01T00:00:00Z',
      createdAt: '2025-01-01T00:00:00Z',
      positionId: 'pos-btc',
      leverageTier: 10,
    };
    pm.updateFromWs(data);
    expect(pm.isFlat).toBe(true);
  });
});
