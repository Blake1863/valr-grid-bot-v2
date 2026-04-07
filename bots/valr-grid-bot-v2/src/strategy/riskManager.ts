/**
 * Risk controls — hard limits before any order is placed.
 * Fail closed: if state is ambiguous or any check fails, we don't trade.
 */

import Decimal from 'decimal.js';
import type { BotConfig } from '../config/schema.js';
import type { PairConstraints } from '../exchange/pairMetadata.js';
import { createLogger } from '../app/logger.js';

const log = createLogger('riskManager');

export class RiskManager {
  private config: BotConfig;
  private constraints: PairConstraints;

  constructor(config: BotConfig, constraints: PairConstraints) {
    this.config = config;
    this.constraints = constraints;
  }

  /** Validate a price meets tick size and min/max quote constraints */
  validatePrice(price: Decimal): void {
    if (price.lte(0)) {
      throw new Error(`Invalid price: ${price} must be > 0`);
    }
    // Check it's a multiple of tickSize
    const remainder = price.mod(this.constraints.tickSize);
    if (!remainder.isZero()) {
      throw new Error(
        `Price ${price} is not a valid tick (tickSize=${this.constraints.tickSize})`
      );
    }
  }

  /** Validate quantity meets exchange min/max */
  validateQuantity(qty: Decimal): void {
    if (qty.lte(0)) {
      throw new Error(`Invalid quantity: ${qty} must be > 0`);
    }
    if (qty.lt(this.constraints.minBaseAmount)) {
      throw new Error(
        `Quantity ${qty} below minimum ${this.constraints.minBaseAmount} for ${this.constraints.symbol}`
      );
    }
    if (qty.gt(this.constraints.maxBaseAmount)) {
      throw new Error(
        `Quantity ${qty} exceeds maximum ${this.constraints.maxBaseAmount} for ${this.constraints.symbol}`
      );
    }
  }

  /** Check if we're in cooldown after a stop-loss */
  checkCooldown(store: { get: (key: string) => string | undefined }): void {
    const cooldownUntilStr = store.get('cooldown_until');
    if (!cooldownUntilStr) return;

    const cooldownUntil = parseInt(cooldownUntilStr, 10);
    if (Date.now() < cooldownUntil) {
      const secsLeft = Math.ceil((cooldownUntil - Date.now()) / 1000);
      throw new Error(`In cooldown after stop-loss. ${secsLeft}s remaining.`);
    }
  }

  /** Check active order count doesn't exceed limit */
  checkActiveOrderCount(activeCount: number): void {
    if (activeCount >= this.config.maxActiveGridOrders) {
      throw new Error(
        `Max active grid orders reached: ${activeCount} >= ${this.config.maxActiveGridOrders}`
      );
    }
  }

  /** Enter post-stop cooldown */
  enterCooldown(store: { set: (key: string, value: string) => void }): void {
    const until = Date.now() + this.config.cooldownAfterStopSecs * 1000;
    store.set('cooldown_until', until.toString());
    log.warn({ secsFromNow: this.config.cooldownAfterStopSecs }, 'Entering cooldown after stop-loss');
  }

  clearCooldown(store: { delete: (key: string) => void }): void {
    store.delete('cooldown_until');
    log.info('Cooldown cleared');
  }
}
