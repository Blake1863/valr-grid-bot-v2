/**
 * SQLite persistence layer using better-sqlite3 (synchronous API).
 *
 * Tables:
 * - grid_orders: active/historical grid limit orders
 * - position_state: current open position snapshot
 * - tpsl_state: active conditional TPSL order
 * - bot_state: k/v store for cooldown, last reconcile, etc.
 */

import Database from 'better-sqlite3';
import { resolve } from 'path';
import { createLogger } from '../app/logger.js';

const log = createLogger('store');

export type OrderStatus = 'pending' | 'placed' | 'filled' | 'cancelled' | 'failed';

export interface GridOrderRow {
  id?: number;
  pair: string;
  side: 'BUY' | 'SELL';
  price: string;
  quantity: string;
  customerOrderId: string;
  exchangeOrderId: string | null;
  status: OrderStatus;
  level: number;
  pairId?: string;       // Grid pair ID (e.g., "pair-1")
  createdAt: string;
  updatedAt: string;
}

export interface PositionStateRow {
  id?: number;
  pair: string;
  side: 'buy' | 'sell';
  quantity: string;
  averageEntryPrice: string;
  positionId: string;
  updatedAt: string;
}

export interface TpslStateRow {
  id?: number;
  pair: string;
  conditionalOrderId: string;
  stopTriggerPrice: string;
  tpTriggerPrice: string | null;
  quantity: string;
  updatedAt: string;
}

export class StateStore {
  private db: Database.Database;

  constructor(dbPath?: string) {
    const path = dbPath ?? resolve(process.cwd(), 'state.db');
    log.info({ path }, 'Opening state database');
    this.db = new Database(path);
    this._init();
  }

  private _init(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS grid_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pair TEXT NOT NULL,
        side TEXT NOT NULL,
        price TEXT NOT NULL,
        quantity TEXT NOT NULL,
        customerOrderId TEXT NOT NULL UNIQUE,
        exchangeOrderId TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        level INTEGER NOT NULL DEFAULT 0,
        pairId TEXT,
        createdAt TEXT NOT NULL,
        updatedAt TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS position_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pair TEXT NOT NULL UNIQUE,
        side TEXT NOT NULL,
        quantity TEXT NOT NULL,
        averageEntryPrice TEXT NOT NULL,
        positionId TEXT NOT NULL,
        updatedAt TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS tpsl_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pair TEXT NOT NULL UNIQUE,
        conditionalOrderId TEXT NOT NULL,
        stopTriggerPrice TEXT NOT NULL,
        tpTriggerPrice TEXT,
        quantity TEXT NOT NULL,
        updatedAt TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS bot_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      );
    `);
  }

  // ─── Grid Orders ──────────────────────────────────────────────────────────

  upsertGridOrder(order: Omit<GridOrderRow, 'id'>): void {
    const stmt = this.db.prepare(`
      INSERT INTO grid_orders (pair, side, price, quantity, customerOrderId, exchangeOrderId, status, level, pairId, createdAt, updatedAt)
      VALUES (@pair, @side, @price, @quantity, @customerOrderId, @exchangeOrderId, @status, @level, @pairId, @createdAt, @updatedAt)
      ON CONFLICT(customerOrderId) DO UPDATE SET
        exchangeOrderId = excluded.exchangeOrderId,
        status = excluded.status,
        pairId = excluded.pairId,
        updatedAt = excluded.updatedAt
    `);
    stmt.run({ ...order, pairId: order.pairId || null });
  }

  updateGridOrderStatus(customerOrderId: string, status: OrderStatus, exchangeOrderId?: string): void {
    const now = new Date().toISOString();
    if (exchangeOrderId !== undefined) {
      this.db
        .prepare(
          'UPDATE grid_orders SET status = ?, exchangeOrderId = ?, updatedAt = ? WHERE customerOrderId = ?'
        )
        .run(status, exchangeOrderId, now, customerOrderId);
    } else {
      this.db
        .prepare('UPDATE grid_orders SET status = ?, updatedAt = ? WHERE customerOrderId = ?')
        .run(status, now, customerOrderId);
    }
  }

  getActiveGridOrders(pair: string): GridOrderRow[] {
    return this.db
      .prepare(
        "SELECT * FROM grid_orders WHERE pair = ? AND status IN ('pending', 'placed') ORDER BY level, side"
      )
      .all(pair) as GridOrderRow[];
  }

  /** Get grid orders with pairId for pair-based reconciliation */
  getGridOrdersWithPairs(pair: string): GridOrderRow[] {
    return this.db
      .prepare(
        "SELECT * FROM grid_orders WHERE pair = ? AND pairId IS NOT NULL ORDER BY pairId, side"
      )
      .all(pair) as GridOrderRow[];
  }

  getGridOrderByCustomerId(customerOrderId: string): GridOrderRow | undefined {
    return this.db
      .prepare('SELECT * FROM grid_orders WHERE customerOrderId = ?')
      .get(customerOrderId) as GridOrderRow | undefined;
  }

  getGridOrderByExchangeId(exchangeOrderId: string): GridOrderRow | undefined {
    return this.db
      .prepare('SELECT * FROM grid_orders WHERE exchangeOrderId = ?')
      .get(exchangeOrderId) as GridOrderRow | undefined;
  }

  clearGridOrdersForPair(pair: string): void {
    this.db.prepare("DELETE FROM grid_orders WHERE pair = ? AND status IN ('pending', 'placed')").run(pair);
  }

  // ─── Position State ───────────────────────────────────────────────────────

  upsertPosition(pos: Omit<PositionStateRow, 'id'>): void {
    this.db
      .prepare(`
        INSERT INTO position_state (pair, side, quantity, averageEntryPrice, positionId, updatedAt)
        VALUES (@pair, @side, @quantity, @averageEntryPrice, @positionId, @updatedAt)
        ON CONFLICT(pair) DO UPDATE SET
          side = excluded.side,
          quantity = excluded.quantity,
          averageEntryPrice = excluded.averageEntryPrice,
          positionId = excluded.positionId,
          updatedAt = excluded.updatedAt
      `)
      .run(pos);
  }

  getPosition(pair: string): PositionStateRow | undefined {
    return this.db.prepare('SELECT * FROM position_state WHERE pair = ?').get(pair) as
      | PositionStateRow
      | undefined;
  }

  clearPosition(pair: string): void {
    this.db.prepare('DELETE FROM position_state WHERE pair = ?').run(pair);
  }

  // ─── TPSL State ───────────────────────────────────────────────────────────

  upsertTpsl(tpsl: Omit<TpslStateRow, 'id'>): void {
    this.db
      .prepare(`
        INSERT INTO tpsl_state (pair, conditionalOrderId, stopTriggerPrice, tpTriggerPrice, quantity, updatedAt)
        VALUES (@pair, @conditionalOrderId, @stopTriggerPrice, @tpTriggerPrice, @quantity, @updatedAt)
        ON CONFLICT(pair) DO UPDATE SET
          conditionalOrderId = excluded.conditionalOrderId,
          stopTriggerPrice = excluded.stopTriggerPrice,
          tpTriggerPrice = excluded.tpTriggerPrice,
          quantity = excluded.quantity,
          updatedAt = excluded.updatedAt
      `)
      .run(tpsl);
  }

  getTpsl(pair: string): TpslStateRow | undefined {
    return this.db.prepare('SELECT * FROM tpsl_state WHERE pair = ?').get(pair) as TpslStateRow | undefined;
  }

  clearTpsl(pair: string): void {
    this.db.prepare('DELETE FROM tpsl_state WHERE pair = ?').run(pair);
  }

  // ─── Bot State (K/V) ──────────────────────────────────────────────────────

  set(key: string, value: string): void {
    this.db
      .prepare('INSERT INTO bot_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value')
      .run(key, value);
  }

  get(key: string): string | undefined {
    const row = this.db.prepare('SELECT value FROM bot_state WHERE key = ?').get(key) as
      | { value: string }
      | undefined;
    return row?.value;
  }

  delete(key: string): void {
    this.db.prepare('DELETE FROM bot_state WHERE key = ?').run(key);
  }
}
