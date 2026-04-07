import { describe, it, expect } from 'vitest';
import Decimal from 'decimal.js';
import { RiskManager } from '../src/strategy/riskManager.js';
import type { BotConfig } from '../src/config/schema.js';
import type { PairConstraints } from '../src/exchange/pairMetadata.js';

const mockConstraints: PairConstraints = {
  symbol: 'SOLUSDTPERP',
  tickSize: new Decimal('0.01'),
  baseDecimalPlaces: 2,
  minBaseAmount: new Decimal('0.1'),
  maxBaseAmount: new Decimal('1000'),
  minQuoteAmount: new Decimal('1'),
  maxQuoteAmount: new Decimal('100000'),
  initialMarginFraction: new Decimal('0.1'),
  maintenanceMarginFraction: new Decimal('0.05'),
  autoCloseMarginFraction: new Decimal('0.033'),
};

const baseConfig: BotConfig = {
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

describe('RiskManager.checkMaxPosition', () => {
  it('allows adding qty within max', () => {
    const rm = new RiskManager(baseConfig, mockConstraints);
    expect(() => rm.checkMaxPosition(new Decimal('3'), new Decimal('1.5'))).not.toThrow();
  });

  it('blocks if new total would exceed max', () => {
    const rm = new RiskManager(baseConfig, mockConstraints);
    expect(() => rm.checkMaxPosition(new Decimal('4'), new Decimal('1.5'))).toThrow(/Max net position exceeded/);
  });

  it('allows exactly at max', () => {
    const rm = new RiskManager(baseConfig, mockConstraints);
    expect(() => rm.checkMaxPosition(new Decimal('3'), new Decimal('1.5'))).not.toThrow(); // 3 + 1.5 = 4.5 = max
  });
});

describe('RiskManager.validateQuantity', () => {
  it('throws below minimum', () => {
    const rm = new RiskManager(baseConfig, mockConstraints);
    expect(() => rm.validateQuantity(new Decimal('0.05'))).toThrow(/below minimum/);
  });

  it('throws above maximum', () => {
    const rm = new RiskManager(baseConfig, mockConstraints);
    expect(() => rm.validateQuantity(new Decimal('10000'))).toThrow(/exceeds maximum/);
  });

  it('throws for zero', () => {
    const rm = new RiskManager(baseConfig, mockConstraints);
    expect(() => rm.validateQuantity(new Decimal('0'))).toThrow(/must be > 0/);
  });

  it('allows valid quantity', () => {
    const rm = new RiskManager(baseConfig, mockConstraints);
    expect(() => rm.validateQuantity(new Decimal('1.5'))).not.toThrow();
  });
});

describe('RiskManager cooldown', () => {
  it('throws if in cooldown', () => {
    const rm = new RiskManager(baseConfig, mockConstraints);
    const store = {
      get: (_k: string) => (Date.now() + 10000).toString(), // future
      set: () => {},
      delete: () => {},
    };
    expect(() => rm.checkCooldown(store)).toThrow(/cooldown/i);
  });

  it('passes if cooldown expired', () => {
    const rm = new RiskManager(baseConfig, mockConstraints);
    const store = {
      get: (_k: string) => (Date.now() - 1000).toString(), // past
      set: () => {},
      delete: () => {},
    };
    expect(() => rm.checkCooldown(store)).not.toThrow();
  });

  it('passes if no cooldown set', () => {
    const rm = new RiskManager(baseConfig, mockConstraints);
    const store = {
      get: (_k: string) => undefined,
      set: () => {},
      delete: () => {},
    };
    expect(() => rm.checkCooldown(store)).not.toThrow();
  });
});
