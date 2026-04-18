/**
 * Grid Pair Manager tests — verify symmetric grid behavior.
 * 
 * Tests cover:
 * - Grid generation with correct pair structure
 * - Pair state transitions
 * - Replenishment logic (replace completed pairs)
 * - Grid stays within configured level range
 * - Recenter trigger on price drift
 */

import { describe, it, expect, beforeEach } from 'vitest';
import Decimal from 'decimal.js';
import {
  buildGridState,
  createGridPair,
  markLegActive,
  markLegFilled,
  markLegMissing,
  getMissingLegs,
  getCompletedPairs,
  getPartialPairs,
  recalculateGridStats,
  needsRecenter,
  rebuildGridWithNewReference,
  replaceCompletedPair,
  calculateQuantityPerLevel,
  type GridState,
} from '../src/strategy/gridPairManager.js';
import type { BotConfig } from '../src/config/schema.js';
import type { PairConstraints } from '../src/exchange/pairMetadata.js';

const mockConstraints: PairConstraints = {
  symbol: 'SOLUSDTPERP',
  tickSize: new Decimal('0.01'),
  baseDecimalPlaces: 2,
  minBaseAmount: new Decimal('0.01'),
  maxBaseAmount: new Decimal('10000'),
  minQuoteAmount: new Decimal('1'),
  maxQuoteAmount: new Decimal('1000000'),
  initialMarginFraction: new Decimal('0.1'),
  maintenanceMarginFraction: new Decimal('0.05'),
  autoCloseMarginFraction: new Decimal('0.033'),
};

const baseConfig: BotConfig = {
  pair: 'SOLUSDTPERP',
  subaccountId: '',
  mode: 'neutral',
  levels: 3,
  spacingMode: 'percent',
  spacingValue: '0.4',
  quantityPerLevel: '1.0',
  dynamicSizing: false,
  targetLeverage: 10,
  capitalAllocationPercent: 90,
  stopLossMode: 'percent',
  stopLossValue: '3.0',
  tpMode: 'disabled',
  triggerType: 'MARK_PRICE',
  referencePriceSource: 'mark_price',
  leverage: 10,
  postOnly: true,
  allowMargin: false,
  cooldownAfterStopSecs: 300,
  dryRun: false,
  reconcileIntervalSecs: 60,
  maxActiveGridOrders: 20,
  wsStaleTimeoutSecs: 30,
};

describe('GridPairManager', () => {
  describe('buildGridState', () => {
    it('creates N pairs with bid + ask legs', () => {
      const refPrice = new Decimal('100');
      const balance = new Decimal('1000');
      const grid = buildGridState(baseConfig, refPrice, mockConstraints, balance, 'test');

      expect(grid.pairs).toHaveLength(3);
      expect(grid.missingPairs).toBe(3);
      expect(grid.totalActiveBids).toBe(0);
      expect(grid.totalActiveAsks).toBe(0);

      // Check pair structure
      for (let i = 0; i < 3; i++) {
        const pair = grid.pairs[i];
        expect(pair.levelIndex).toBe(i + 1);
        expect(pair.pairId).toBe(`pair-${i + 1}`);
        expect(pair.bidLeg.side).toBe('BUY');
        expect(pair.askLeg.side).toBe('SELL');
        expect(pair.bidLeg.state).toBe('missing');
        expect(pair.askLeg.state).toBe('missing');
      }
    });

    it('calculates correct prices with percent spacing', () => {
      const refPrice = new Decimal('100');
      const balance = new Decimal('1000');
      const grid = buildGridState(baseConfig, refPrice, mockConstraints, balance, 'test');

      // Level 1: 100 * 0.996 = 99.60 (bid), 100 * 1.004 = 100.40 (ask)
      expect(grid.pairs[0].bidLeg.priceStr).toBe('99.60');
      expect(grid.pairs[0].askLeg.priceStr).toBe('100.40');

      // Level 2: 100 * 0.996^2 = 99.20, 100 * 1.004^2 = 100.80
      expect(grid.pairs[1].bidLeg.priceStr).toBe('99.20');
      expect(grid.pairs[1].askLeg.priceStr).toBe('100.80');

      // Level 3: 100 * 0.996^3 = 98.81, 100 * 1.004^3 = 101.21
      expect(grid.pairs[2].bidLeg.priceStr).toBe('98.81');
      expect(grid.pairs[2].askLeg.priceStr).toBe('101.20');
    });

    it('calculates dynamic quantity from balance', () => {
      const refPrice = new Decimal('100');
      const balance = new Decimal('1000');
      const config: BotConfig = { ...baseConfig, dynamicSizing: true, targetLeverage: 10 };
      const grid = buildGridState(config, refPrice, mockConstraints, balance, 'test');

      // Expected: 1000 * 10 * 0.9 / 6 levels / 100 price = 1.5 per level
      const expectedQty = '1.50';
      expect(grid.pairs[0].bidLeg.quantityStr).toBe(expectedQty);
      expect(grid.pairs[0].askLeg.quantityStr).toBe(expectedQty);
    });
  });

  describe('markLegActive', () => {
    let grid: GridState;

    beforeEach(() => {
      const refPrice = new Decimal('100');
      const balance = new Decimal('1000');
      grid = buildGridState(baseConfig, refPrice, mockConstraints, balance, 'test');
    });

    it('marks bid leg as active', () => {
      const result = markLegActive(grid, grid.pairs[0].bidLeg.customerOrderId, 'order-123');
      
      expect(result).toBe(true);
      expect(grid.pairs[0].bidLeg.state).toBe('active');
      expect(grid.pairs[0].bidLeg.exchangeOrderId).toBe('order-123');
      expect(grid.pairs[0].state).toBe('active');
      expect(grid.totalActiveBids).toBe(1);
    });

    it('marks ask leg as active', () => {
      const result = markLegActive(grid, grid.pairs[0].askLeg.customerOrderId, 'order-456');
      
      expect(result).toBe(true);
      expect(grid.pairs[0].askLeg.state).toBe('active');
      expect(grid.pairs[0].state).toBe('active');
      expect(grid.totalActiveAsks).toBe(1);
    });

    it('returns false for unknown customerOrderId', () => {
      const result = markLegActive(grid, 'unknown-order', 'order-789');
      expect(result).toBe(false);
    });
  });

  describe('markLegFilled', () => {
    let grid: GridState;

    beforeEach(() => {
      const refPrice = new Decimal('100');
      const balance = new Decimal('1000');
      grid = buildGridState(baseConfig, refPrice, mockConstraints, balance, 'test');
      
      // Activate both legs first
      markLegActive(grid, grid.pairs[0].bidLeg.customerOrderId, 'bid-123');
      markLegActive(grid, grid.pairs[0].askLeg.customerOrderId, 'ask-456');
    });

    it('marks bid as filled and pair as partial', () => {
      const pair = markLegFilled(grid, undefined, 'bid-123');
      
      expect(pair).toBe(grid.pairs[0]);
      expect(grid.pairs[0].bidLeg.state).toBe('filled');
      expect(grid.pairs[0].askLeg.state).toBe('active');
      expect(grid.pairs[0].state).toBe('partial');
      expect(grid.partialPairs).toBe(1);
    });

    it('marks pair as complete when both legs filled', () => {
      markLegFilled(grid, undefined, 'bid-123');
      const pair = markLegFilled(grid, undefined, 'ask-456');
      
      expect(pair).toBe(grid.pairs[0]);
      expect(grid.pairs[0].state).toBe('complete');
      expect(grid.pairs[0].completedAt).toBeDefined();
      expect(grid.completePairs).toBe(1);
    });
  });

  describe('getMissingLegs', () => {
    it('returns all legs when grid is empty', () => {
      const refPrice = new Decimal('100');
      const balance = new Decimal('1000');
      const grid = buildGridState(baseConfig, refPrice, mockConstraints, balance, 'test');
      
      const missing = getMissingLegs(grid);
      expect(missing).toHaveLength(6); // 3 pairs * 2 legs
    });

    it('returns only missing legs after some placed', () => {
      const refPrice = new Decimal('100');
      const balance = new Decimal('1000');
      const grid = buildGridState(baseConfig, refPrice, mockConstraints, balance, 'test');
      
      markLegActive(grid, grid.pairs[0].bidLeg.customerOrderId, 'order-1');
      markLegActive(grid, grid.pairs[0].askLeg.customerOrderId, 'order-2');
      recalculateGridStats(grid);
      
      const missing = getMissingLegs(grid);
      expect(missing).toHaveLength(4); // 2 remaining pairs * 2 legs
    });
  });

  describe('replaceCompletedPair', () => {
    it('replaces completed pair with fresh pair at same level', () => {
      const refPrice = new Decimal('100');
      const balance = new Decimal('1000');
      const grid = buildGridState(baseConfig, refPrice, mockConstraints, balance, 'test');
      
      // Activate and fill pair 1
      markLegActive(grid, grid.pairs[0].bidLeg.customerOrderId, 'bid-1');
      markLegActive(grid, grid.pairs[0].askLeg.customerOrderId, 'ask-1');
      markLegFilled(grid, undefined, 'bid-1');
      markLegFilled(grid, undefined, 'ask-1');
      
      expect(grid.pairs[0].state).toBe('complete');
      
      // Replace the pair
      const qty = new Decimal('1.0');
      const newPair = replaceCompletedPair(grid, 'pair-1', baseConfig, refPrice, mockConstraints, qty, 'new-seed');
      
      expect(newPair).toBeTruthy();
      expect(newPair!.levelIndex).toBe(1);
      expect(newPair!.bidLeg.state).toBe('missing');
      expect(newPair!.askLeg.state).toBe('missing');
      expect(newPair!.pairId).toBe('pair-1');
      
      // Grid should have missing pairs again
      expect(grid.missingPairs).toBe(1);
      expect(grid.completePairs).toBe(0);
    });
  });

  describe('needsRecenter', () => {
    it('returns false when price is within grid range', () => {
      const refPrice = new Decimal('100');
      const balance = new Decimal('1000');
      const grid = buildGridState(baseConfig, refPrice, mockConstraints, balance, 'test');
      
      // Small price move (1%) - should not trigger recenter
      const newPrice = new Decimal('101');
      expect(needsRecenter(grid, newPrice, baseConfig, mockConstraints)).toBe(false);
    });

    it('returns true when price moves beyond threshold', () => {
      const refPrice = new Decimal('100');
      const balance = new Decimal('1000');
      const grid = buildGridState(baseConfig, refPrice, mockConstraints, balance, 'test');
      
      // Large price move (10%) - should trigger recenter
      const newPrice = new Decimal('110');
      expect(needsRecenter(grid, newPrice, baseConfig, mockConstraints)).toBe(true);
    });
  });

  describe('rebuildGridWithNewReference', () => {
    it('rebuilds grid with new reference price', () => {
      const refPrice = new Decimal('100');
      const balance = new Decimal('1000');
      let grid = buildGridState(baseConfig, refPrice, mockConstraints, balance, 'test');
      
      // Activate some legs
      markLegActive(grid, grid.pairs[0].bidLeg.customerOrderId, 'bid-1');
      markLegActive(grid, grid.pairs[0].askLeg.customerOrderId, 'ask-1');
      recalculateGridStats(grid);
      
      // Rebuild with new reference
      const newPrice = new Decimal('105');
      const newGrid = rebuildGridWithNewReference(grid, baseConfig, newPrice, mockConstraints, balance, 'new-seed');
      
      expect(newGrid.referencePrice).toEqual(newPrice);
      expect(newGrid.pairs).toHaveLength(3);
      
      // Prices should be recalculated around new reference
      expect(newGrid.pairs[0].bidLeg.price).toBeLessThan(newPrice);
      expect(newGrid.pairs[0].askLeg.price).toBeGreaterThan(newPrice);
    });

    it('preserves filled legs during rebuild', () => {
      const refPrice = new Decimal('100');
      const balance = new Decimal('1000');
      let grid = buildGridState(baseConfig, refPrice, mockConstraints, balance, 'test');
      
      // Fill one leg (simulating open position)
      markLegActive(grid, grid.pairs[0].bidLeg.customerOrderId, 'bid-1');
      markLegFilled(grid, undefined, 'bid-1');
      
      const newPrice = new Decimal('105');
      const newGrid = rebuildGridWithNewReference(grid, baseConfig, newPrice, mockConstraints, balance, 'new-seed');
      
      // Filled leg should be preserved
      expect(newGrid.pairs[0].bidLeg.state).toBe('filled');
      // Missing leg should be recalculated
      expect(newGrid.pairs[0].askLeg.state).toBe('missing');
    });
  });

  describe('Grid stays within configured range', () => {
    it('never exceeds N levels per side', () => {
      const refPrice = new Decimal('100');
      const balance = new Decimal('1000');
      const grid = buildGridState(baseConfig, refPrice, mockConstraints, balance, 'test');
      
      // Simulate multiple fills and replacements
      for (let i = 0; i < 10; i++) {
        // Fill pair 1
        markLegActive(grid, grid.pairs[0].bidLeg.customerOrderId, `bid-${i}-1`);
        markLegActive(grid, grid.pairs[0].askLeg.customerOrderId, `ask-${i}-1`);
        markLegFilled(grid, undefined, `bid-${i}-1`);
        markLegFilled(grid, undefined, `ask-${i}-1`);
        
        // Replace
        const qty = new Decimal('1.0');
        replaceCompletedPair(grid, 'pair-1', baseConfig, refPrice, mockConstraints, qty, `seed-${i}`);
      }
      
      // Should still have exactly 3 pairs
      expect(grid.pairs).toHaveLength(3);
      
      // All pairs should be at levels 1, 2, 3 (no expansion)
      const levels = grid.pairs.map(p => p.levelIndex);
      expect(levels).toEqual([1, 2, 3]);
    });

    it('maintains symmetric structure after fills', () => {
      const refPrice = new Decimal('100');
      const balance = new Decimal('1000');
      const grid = buildGridState(baseConfig, refPrice, mockConstraints, balance, 'test');
      
      // Fill only bids on all levels (price going down)
      for (const pair of grid.pairs) {
        markLegActive(grid, pair.bidLeg.customerOrderId, `bid-${pair.levelIndex}`);
        markLegActive(grid, pair.askLeg.customerOrderId, `ask-${pair.levelIndex}`);
        markLegFilled(grid, undefined, `bid-${pair.levelIndex}`);
      }
      
      recalculateGridStats(grid);
      
      // Should have 3 partial pairs (bid filled, ask still active)
      expect(grid.partialPairs).toBe(3);
      expect(grid.completePairs).toBe(0);
      
      // Grid structure is still symmetric - asks are still there
      expect(grid.totalActiveAsks).toBe(3);
    });
  });

  describe('calculateQuantityPerLevel', () => {
    it('calculates correct quantity for symmetric grid', () => {
      const refPrice = new Decimal('100');
      const balance = new Decimal('1000');
      const config: BotConfig = { ...baseConfig, dynamicSizing: true, targetLeverage: 10 };
      
      const qty = calculateQuantityPerLevel(config, balance, refPrice, mockConstraints);
      
      // Expected: 1000 * 10 * 0.9 / 6 / 100 = 1.5
      expect(qty.toString()).toBe('1.50');
    });

    it('respects minimum order size', () => {
      const refPrice = new Decimal('100');
      const balance = new Decimal('10'); // Very small balance
      const config: BotConfig = { ...baseConfig, dynamicSizing: true, targetLeverage: 10 };
      
      const qty = calculateQuantityPerLevel(config, balance, refPrice, mockConstraints);
      
      // Should be at least minBaseAmount (0.01)
      expect(qty.greaterThanOrEqualTo(mockConstraints.minBaseAmount)).toBe(true);
    });
  });
});
