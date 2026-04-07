/**
 * Position state manager.
 * Exchange state is authoritative — always update from REST or WS events.
 */

import Decimal from 'decimal.js';
import type { ValrOpenPosition, WsOpenPositionUpdate, WsPositionClosed } from '../exchange/types.js';
import type { StateStore, PositionStateRow } from '../state/store.js';
import { createLogger } from '../app/logger.js';

const log = createLogger('positionManager');

export interface PositionState {
  pair: string;
  side: 'buy' | 'sell';
  quantity: Decimal; // positive always
  averageEntryPrice: Decimal; // SOURCE OF TRUTH for SL/TP
  positionId: string;
  updatedAt: string;
}

export class PositionManager {
  private pair: string;
  private store: StateStore;
  private _position: PositionState | null = null;

  constructor(pair: string, store: StateStore) {
    this.pair = pair;
    this.store = store;
  }

  get position(): PositionState | null {
    return this._position;
  }

  get isFlat(): boolean {
    return this._position === null || this._position.quantity.isZero();
  }

  get netQuantity(): Decimal {
    if (this.isFlat) return new Decimal(0);
    return this._position!.quantity;
  }

  /** Restore position from database on startup */
  loadFromStore(): void {
    const row = this.store.getPosition(this.pair);
    if (row) {
      this._position = this._rowToState(row);
      log.info(
        { side: row.side, qty: row.quantity, avg: row.averageEntryPrice },
        'Position loaded from store'
      );
    }
  }

  /** Update from REST GET /v1/positions/open — authoritative */
  updateFromRest(positions: ValrOpenPosition[]): void {
    const pos = positions.find((p) => p.pair === this.pair);
    if (!pos) {
      if (this._position !== null) {
        log.info({ pair: this.pair }, 'REST shows no position — clearing local state');
      }
      this._position = null;
      this.store.clearPosition(this.pair);
      return;
    }

    const qty = new Decimal(pos.quantity);
    if (qty.isZero()) {
      this._position = null;
      this.store.clearPosition(this.pair);
      return;
    }

    this._position = {
      pair: pos.pair,
      side: pos.side,
      quantity: qty,
      averageEntryPrice: new Decimal(pos.averageEntryPrice),
      positionId: pos.positionId,
      updatedAt: pos.updatedAt,
    };

    this._persist();
    log.info(
      { side: pos.side, qty: pos.quantity, avg: pos.averageEntryPrice },
      'Position updated from REST'
    );
  }

  /**
   * Update from WS OPEN_POSITION_UPDATE event.
   * @returns true if averageEntryPrice changed (caller should rebuild TPSL)
   */
  updateFromWs(data: WsOpenPositionUpdate): boolean {
    if (data.pair !== this.pair) return false;

    const qty = new Decimal(data.quantity);
    if (qty.isZero()) {
      this._position = null;
      this.store.clearPosition(this.pair);
      log.info({ pair: this.pair }, 'Position cleared (qty=0 from WS)');
      return false;
    }

    const newEntryPrice = new Decimal(data.averageEntryPrice);
    const entryPriceChanged = !this._position || !this._position.averageEntryPrice.eq(newEntryPrice);

    this._position = {
      pair: data.pair,
      side: data.side,
      quantity: qty,
      averageEntryPrice: newEntryPrice, // SOURCE OF TRUTH
      positionId: data.positionId,
      updatedAt: data.updatedAt,
    };

    this._persist();
    log.info(
      { side: data.side, qty: data.quantity, avg: data.averageEntryPrice, entryPriceChanged },
      'Position updated from WS'
    );

    return entryPriceChanged;
  }

  /** Handle WS POSITION_CLOSED event */
  handlePositionClosed(data: WsPositionClosed): void {
    if (data.pair !== this.pair) return;
    log.info({ pair: data.pair, positionId: data.positionId }, 'Position CLOSED');
    this._position = null;
    this.store.clearPosition(this.pair);
  }

  private _persist(): void {
    if (!this._position) return;
    this.store.upsertPosition({
      pair: this._position.pair,
      side: this._position.side,
      quantity: this._position.quantity.toString(),
      averageEntryPrice: this._position.averageEntryPrice.toString(),
      positionId: this._position.positionId,
      updatedAt: this._position.updatedAt,
    });
  }

  private _rowToState(row: PositionStateRow): PositionState {
    return {
      pair: row.pair,
      side: row.side,
      quantity: new Decimal(row.quantity),
      averageEntryPrice: new Decimal(row.averageEntryPrice),
      positionId: row.positionId,
      updatedAt: row.updatedAt,
    };
  }
}
