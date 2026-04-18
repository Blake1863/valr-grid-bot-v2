/**
 * TPSL Manager
 * Places/cancels/rebuilds futures conditional TPSL orders via POST /v1/orders/conditionals
 *
 * Key rules:
 * - quantity "0" = close entire position (per docs)
 * - stopLossOrderPrice = "-1" = market execution on trigger
 * - takeProfitOrderPrice = "-1" = market execution on trigger
 * - If both TP and SL provided → OCO (one cancels other)
 * - 202 Accepted ≠ order live — confirm via WS or GET /v1/orders/conditionals
 * - Always cancel existing TPSL before placing new one
 */

import Decimal from 'decimal.js';
import type { ValrRestClient } from '../exchange/restClient.js';
import type { BotConfig } from '../config/schema.js';
import type { PositionState } from './positionManager.js';
import type { StateStore } from '../state/store.js';
// TPSL uses position-based SL, not grid spacing
import { calcSlPrice } from './gridBuilder.js';
import { priceToString } from '../exchange/pairMetadata.js';
import type { PairConstraints } from '../exchange/pairMetadata.js';
import { createLogger } from '../app/logger.js';

const log = createLogger('tpslManager');

export class TpslManager {
  private rest: ValrRestClient;
  private config: BotConfig;
  private constraints: PairConstraints;
  private store: StateStore;
  private dryRun: boolean;

  constructor(
    rest: ValrRestClient,
    config: BotConfig,
    constraints: PairConstraints,
    store: StateStore
  ) {
    this.rest = rest;
    this.config = config;
    this.constraints = constraints;
    this.store = store;
    this.dryRun = config.dryRun;
  }

  /**
   * Cancel any existing TPSL for this pair, then place fresh one based on current position.
   * Called on: startup (if position exists), after fill, after position update.
   * Throws if SL placement fails (caller should halt new entries).
   */
  async rebuild(position: PositionState, referencePrice: Decimal): Promise<void> {
    const pair = this.config.pair;

    // Cancel existing TPSL first
    await this._cancelExisting();

    const entry = position.averageEntryPrice;
    // Pass position side so neutral mode SL direction is correct
    const slPrice = calcSlPrice(this.config, entry, position.side);
    // For neutral mode, TP is disabled (grid levels are the TP)
    const tpPrice = null;

    const slStr = priceToString(slPrice, this.constraints.tickSize);
    const tpStr = tpPrice ? priceToString(tpPrice, this.constraints.tickSize) : null;

    log.info(
      {
        pair,
        entry: entry.toString(),
        sl: slStr,
        tp: tpStr ?? 'disabled',
        mode: this.config.mode,
        stopLossMode: this.config.stopLossMode,
      },
      'Placing TPSL'
    );

    const seed = Date.now().toString(36);
    const customerOrderId = `tpsl-${seed}`.slice(0, 50);

    const orderPayload = {
      pair,
      quantity: '0', // close entire position
      triggerType: this.config.triggerType,
      customerOrderId,
      stopLossTriggerPrice: slStr,
      stopLossOrderPrice: '-1', // market execution
      ...(tpStr
        ? {
            takeProfitTriggerPrice: tpStr,
            takeProfitOrderPrice: '-1',
          }
        : {}),
    };

    if (this.dryRun) {
      log.info({ orderPayload }, '[DRY-RUN] Would place TPSL conditional order');
      this.store.upsertTpsl({
        pair,
        conditionalOrderId: 'dry-run-tpsl',
        stopTriggerPrice: slStr,
        tpTriggerPrice: tpStr ?? null,
        quantity: '0',
        updatedAt: new Date().toISOString(),
      });
      return;
    }

    try {
      const resp = await this.rest.placeConditionalOrder(orderPayload);
      log.info({ id: resp.id }, 'TPSL conditional order accepted (202)');

      // Persist so we can cancel on next rebuild
      this.store.upsertTpsl({
        pair,
        conditionalOrderId: resp.id,
        stopTriggerPrice: slStr,
        tpTriggerPrice: tpStr ?? null,
        quantity: '0',
        updatedAt: new Date().toISOString(),
      });
    } catch (err) {
      log.error({ err, pair }, 'CRITICAL: Failed to place TPSL conditional order');
      throw err; // Caller must halt entries
    }
  }

  /** Cancel TPSL when position is closed or bot stops */
  async cancelAll(): Promise<void> {
    await this._cancelExisting();
    this.store.clearTpsl(this.config.pair);
  }

  private async _cancelExisting(): Promise<void> {
    const pair = this.config.pair;

    // Cancel tracked conditional
    const existing = this.store.getTpsl(pair);
    if (existing?.conditionalOrderId && existing.conditionalOrderId !== 'dry-run-tpsl') {
      await this.rest.cancelConditionalOrder(existing.conditionalOrderId, pair);
    }

    // Also nuke any stale conditionals for the pair on exchange
    // (e.g. from a previous run that crashed before persisting)
    await this.rest.cancelAllConditionalsForPair(pair);

    this.store.clearTpsl(pair);
  }
}
