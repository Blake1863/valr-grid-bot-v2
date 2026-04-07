import { describe, it, expect } from 'vitest';
import Decimal from 'decimal.js';
import {
  buildGridLevels,
  buildReplenishLevel,
  calcSlPrice,
  calcTpPrice,
  getSpacingAmount,
} from '../src/strategy/gridBuilder.js';
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
  subaccountId: '1234',
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

// ─── long_only ────────────────────────────────────────────────────────────────

describe('buildGridLevels — long_only percent spacing', () => {
  it('generates correct number of levels', () => {
    const levels = buildGridLevels(baseConfig, new Decimal('100'), mockConstraints, 'test');
    expect(levels).toHaveLength(3);
  });

  it('all levels are BUY', () => {
    const levels = buildGridLevels(baseConfig, new Decimal('100'), mockConstraints, 'test');
    for (const l of levels) expect(l.side).toBe('BUY');
  });

  it('prices decrease for each deeper level', () => {
    const levels = buildGridLevels(baseConfig, new Decimal('100'), mockConstraints, 'test');
    for (let i = 1; i < levels.length; i++) {
      expect(levels[i].price.lt(levels[i - 1].price)).toBe(true);
    }
  });

  it('level 1 price = ref * (1 - 0.004)', () => {
    const ref = new Decimal('100');
    const levels = buildGridLevels(baseConfig, ref, mockConstraints, 'test');
    expect(levels[0].price.toFixed(2)).toBe('99.60');
  });

  it('prices are valid multiples of tickSize', () => {
    const levels = buildGridLevels(baseConfig, new Decimal('120.50'), mockConstraints, 'test');
    for (const l of levels) {
      const remainder = new Decimal(l.priceStr).mod(mockConstraints.tickSize);
      expect(remainder.isZero()).toBe(true);
    }
  });

  it('quantities truncated to baseDecimalPlaces', () => {
    const levels = buildGridLevels(baseConfig, new Decimal('100'), mockConstraints, 'test');
    for (const l of levels) {
      const dp = l.quantityStr.includes('.') ? l.quantityStr.split('.')[1].length : 0;
      expect(dp).toBeLessThanOrEqual(mockConstraints.baseDecimalPlaces);
    }
  });

  it('customerOrderId max 50 chars', () => {
    const levels = buildGridLevels(baseConfig, new Decimal('100'), mockConstraints, 'test');
    for (const l of levels) expect(l.customerOrderId.length).toBeLessThanOrEqual(50);
  });
});

describe('buildGridLevels — long_only absolute spacing', () => {
  const absConfig: BotConfig = { ...baseConfig, spacingMode: 'absolute', spacingValue: '0.50' };

  it('level 1 = ref - spacing', () => {
    const levels = buildGridLevels(absConfig, new Decimal('100'), mockConstraints, 'test');
    expect(levels[0].price.toString()).toBe('99.5');
  });

  it('level 2 = ref - 2*spacing', () => {
    const levels = buildGridLevels(absConfig, new Decimal('100'), mockConstraints, 'test');
    expect(levels[1].price.toString()).toBe('99');
  });
});

// ─── short_only ────────────────────────────────────────────────────────────────

describe('buildGridLevels — short_only', () => {
  const shortConfig: BotConfig = { ...baseConfig, mode: 'short_only' };

  it('all levels are SELL', () => {
    const levels = buildGridLevels(shortConfig, new Decimal('100'), mockConstraints, 'test');
    for (const l of levels) expect(l.side).toBe('SELL');
  });

  it('prices increase for short_only percent spacing', () => {
    const levels = buildGridLevels(shortConfig, new Decimal('100'), mockConstraints, 'test');
    for (let i = 1; i < levels.length; i++) {
      expect(levels[i].price.gt(levels[i - 1].price)).toBe(true);
    }
  });
});

// ─── neutral ────────────────────────────────────────────────────────────────

describe('buildGridLevels — neutral (symmetric)', () => {
  const neutralConfig: BotConfig = { ...baseConfig, mode: 'neutral', tpMode: 'disabled' };

  it('generates 2*levels total orders', () => {
    const levels = buildGridLevels(neutralConfig, new Decimal('100'), mockConstraints, 'test');
    expect(levels).toHaveLength(6); // 3 buys + 3 sells
  });

  it('has equal number of BUY and SELL orders', () => {
    const levels = buildGridLevels(neutralConfig, new Decimal('100'), mockConstraints, 'test');
    const buys = levels.filter((l) => l.side === 'BUY');
    const sells = levels.filter((l) => l.side === 'SELL');
    expect(buys.length).toBe(3);
    expect(sells.length).toBe(3);
  });

  it('all BUYs are below reference price', () => {
    const ref = new Decimal('100');
    const levels = buildGridLevels(neutralConfig, ref, mockConstraints, 'test');
    for (const l of levels.filter((l) => l.side === 'BUY')) {
      expect(l.price.lt(ref)).toBe(true);
    }
  });

  it('all SELLs are above reference price', () => {
    const ref = new Decimal('100');
    const levels = buildGridLevels(neutralConfig, ref, mockConstraints, 'test');
    for (const l of levels.filter((l) => l.side === 'SELL')) {
      expect(l.price.gt(ref)).toBe(true);
    }
  });

  it('BUY and SELL levels are symmetric around reference', () => {
    const ref = new Decimal('100');
    const levels = buildGridLevels(neutralConfig, ref, mockConstraints, 'test');
    const buys = levels.filter((l) => l.side === 'BUY');
    const sells = levels.filter((l) => l.side === 'SELL');
    for (let i = 0; i < buys.length; i++) {
      const buyDist = ref.minus(buys[i].price);
      const sellDist = sells[i].price.minus(ref);
      // Should be approximately equal (slight diff due to percent compounding)
      expect(buyDist.minus(sellDist).abs().lt(new Decimal('0.1'))).toBe(true);
    }
  });

  it('customerOrderIds distinguish BUY from SELL', () => {
    const levels = buildGridLevels(neutralConfig, new Decimal('100'), mockConstraints, 'test');
    const buyIds = levels.filter((l) => l.side === 'BUY').map((l) => l.customerOrderId);
    const sellIds = levels.filter((l) => l.side === 'SELL').map((l) => l.customerOrderId);
    // No overlap
    for (const id of buyIds) expect(sellIds).not.toContain(id);
  });
});

describe('buildReplenishLevel — neutral replenishment', () => {
  const neutralConfig: BotConfig = { ...baseConfig, mode: 'neutral', tpMode: 'disabled' };

  it('BUY replenishment is below reference price', () => {
    const ref = new Decimal('100');
    const level = buildReplenishLevel(neutralConfig, ref, mockConstraints, 'BUY', 4, 'seed');
    expect(level.side).toBe('BUY');
    expect(level.price.lt(ref)).toBe(true);
  });

  it('SELL replenishment is above reference price', () => {
    const ref = new Decimal('100');
    const level = buildReplenishLevel(neutralConfig, ref, mockConstraints, 'SELL', 4, 'seed');
    expect(level.side).toBe('SELL');
    expect(level.price.gt(ref)).toBe(true);
  });

  it('deeper level = deeper price', () => {
    const ref = new Decimal('100');
    const level3 = buildReplenishLevel(neutralConfig, ref, mockConstraints, 'BUY', 3, 'seed');
    const level4 = buildReplenishLevel(neutralConfig, ref, mockConstraints, 'BUY', 4, 'seed');
    expect(level4.price.lt(level3.price)).toBe(true);
  });
});

// ─── SL price calc ────────────────────────────────────────────────────────────

describe('calcSlPrice', () => {
  it('long_only percent: entry * (1 - pct%)', () => {
    expect(calcSlPrice(baseConfig, new Decimal('100')).toString()).toBe('97');
  });

  it('short_only percent: entry * (1 + pct%)', () => {
    const sc: BotConfig = { ...baseConfig, mode: 'short_only' };
    expect(calcSlPrice(sc, new Decimal('100')).toString()).toBe('103');
  });

  it('neutral with buy position: SL below entry', () => {
    const nc: BotConfig = { ...baseConfig, mode: 'neutral' };
    const sl = calcSlPrice(nc, new Decimal('100'), 'buy');
    expect(sl.lt(new Decimal('100'))).toBe(true);
    expect(sl.toString()).toBe('97');
  });

  it('neutral with sell position: SL above entry', () => {
    const nc: BotConfig = { ...baseConfig, mode: 'neutral' };
    const sl = calcSlPrice(nc, new Decimal('100'), 'sell');
    expect(sl.gt(new Decimal('100'))).toBe(true);
    expect(sl.toString()).toBe('103');
  });

  it('long_only absolute: entry - value', () => {
    const ac: BotConfig = { ...baseConfig, stopLossMode: 'absolute', stopLossValue: '5' };
    expect(calcSlPrice(ac, new Decimal('100')).toString()).toBe('95');
  });

  it('short_only absolute: entry + value', () => {
    const ac: BotConfig = { ...baseConfig, mode: 'short_only', stopLossMode: 'absolute', stopLossValue: '5' };
    expect(calcSlPrice(ac, new Decimal('100')).toString()).toBe('105');
  });
});

// ─── TP price calc ────────────────────────────────────────────────────────────

describe('calcTpPrice', () => {
  it('returns null when disabled', () => {
    const nc: BotConfig = { ...baseConfig, tpMode: 'disabled' };
    expect(calcTpPrice(nc, new Decimal('100'), new Decimal('1'))).toBeNull();
  });

  it('returns null for neutral mode (grid is its own TP)', () => {
    const nc: BotConfig = { ...baseConfig, mode: 'neutral', tpMode: 'one_level' };
    expect(calcTpPrice(nc, new Decimal('100'), new Decimal('1'))).toBeNull();
  });

  it('one_level long: entry + spacing', () => {
    const result = calcTpPrice(baseConfig, new Decimal('100'), new Decimal('2'));
    expect(result!.toString()).toBe('102');
  });

  it('one_level short: entry - spacing', () => {
    const sc: BotConfig = { ...baseConfig, mode: 'short_only' };
    expect(calcTpPrice(sc, new Decimal('100'), new Decimal('2'))!.toString()).toBe('98');
  });

  it('fixed long: entry + fixedValue', () => {
    const fc: BotConfig = { ...baseConfig, tpMode: 'fixed', tpFixedValue: '5' };
    expect(calcTpPrice(fc, new Decimal('100'), new Decimal('1'))!.toString()).toBe('105');
  });
});
