/**
 * State Store — SQLite Persistence
 * 
 * Persists grid state, completed cycles, and order mappings across restarts.
 */

import Database from 'better-sqlite3';
import path from 'path';
import { fileURLToPath } from 'url';
import { createLogger } from '../app/logger.js';
import type { GridCycle } from '../strategy/gridManager.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const log = createLogger('store');

export interface StoredOrder {
  levelIndex: number;
  customerOrderId: string;
  exchangeOrderId?: string;
  side: 'BUY' | 'SELL';
  price: string;
  quantity: string;
  role: 'entry' | 'exit';
  state: string;
  createdAt: string;
}

export interface StoredCycle {
  cycleId: string;
  entryLevelIndex: number;
  exitLevelIndex: number;
  entrySide: 'BUY' | 'SELL';
  entryPrice: string;
  exitPrice: string;
  quantity: string;
  realizedProfit: string;
  completedAt: string;
}

export class StateStore {
  private db: Database.Database;

  constructor(dbPath: string) {
    this.db = new Database(dbPath);
    this.initSchema();
  }

  private initSchema(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        levelIndex INTEGER NOT NULL,
        customerOrderId TEXT UNIQUE NOT NULL,
        exchangeOrderId TEXT,
        side TEXT NOT NULL,
        price TEXT NOT NULL,
        quantity TEXT NOT NULL,
        role TEXT NOT NULL,
        state TEXT NOT NULL,
        createdAt TEXT NOT NULL,
        updatedAt TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS cycles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycleId TEXT UNIQUE NOT NULL,
        entryLevelIndex INTEGER NOT NULL,
        exitLevelIndex INTEGER NOT NULL,
        entrySide TEXT NOT NULL,
        entryPrice TEXT NOT NULL,
        exitPrice TEXT NOT NULL,
        quantity TEXT NOT NULL,
        realizedProfit TEXT NOT NULL,
        completedAt TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updatedAt TEXT NOT NULL
      );

      CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(state);
      CREATE INDEX IF NOT EXISTS idx_cycles_completedAt ON cycles(completedAt);
    `);
  }

  // === Orders ===

  saveOrder(order: StoredOrder): void {
    const stmt = this.db.prepare(`
      INSERT OR REPLACE INTO orders 
      (levelIndex, customerOrderId, exchangeOrderId, side, price, quantity, role, state, createdAt, updatedAt)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `);

    stmt.run(
      order.levelIndex,
      order.customerOrderId,
      order.exchangeOrderId || null,
      order.side,
      order.price,
      order.quantity,
      order.role,
      order.state,
      order.createdAt,
      new Date().toISOString()
    );
  }

  getOrders(): StoredOrder[] {
    const stmt = this.db.prepare('SELECT * FROM orders ORDER BY levelIndex');
    return stmt.all() as StoredOrder[];
  }

  updateOrderState(customerOrderId: string, state: string, exchangeOrderId?: string): void {
    const stmt = this.db.prepare(`
      UPDATE orders 
      SET state = ?, exchangeOrderId = ?, updatedAt = ?
      WHERE customerOrderId = ?
    `);
    stmt.run(state, exchangeOrderId || null, new Date().toISOString(), customerOrderId);
  }

  deleteOrder(customerOrderId: string): void {
    const stmt = this.db.prepare('DELETE FROM orders WHERE customerOrderId = ?');
    stmt.run(customerOrderId);
  }

  clearOrders(): void {
    this.db.exec('DELETE FROM orders');
  }

  // === Cycles ===

  saveCycle(cycle: StoredCycle): void {
    const stmt = this.db.prepare(`
      INSERT OR IGNORE INTO cycles
      (cycleId, entryLevelIndex, exitLevelIndex, entrySide, entryPrice, exitPrice, quantity, realizedProfit, completedAt)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    `);
    stmt.run(
      cycle.cycleId,
      cycle.entryLevelIndex,
      cycle.exitLevelIndex,
      cycle.entrySide,
      cycle.entryPrice,
      cycle.exitPrice,
      cycle.quantity,
      cycle.realizedProfit,
      cycle.completedAt
    );
  }

  getCycles(limit?: number): StoredCycle[] {
    const sql = limit 
      ? 'SELECT * FROM cycles ORDER BY completedAt DESC LIMIT ?'
      : 'SELECT * FROM cycles ORDER BY completedAt DESC';
    const stmt = this.db.prepare(sql);
    return limit ? (stmt.all(limit) as StoredCycle[]) : (stmt.all() as StoredCycle[]);
  }

  getTotalRealizedProfit(): string {
    const stmt = this.db.prepare('SELECT SUM(realizedProfit) as total FROM cycles');
    const result = stmt.get() as { total: string | null };
    return result.total || '0';
  }

  // === Generic State ===

  setState(key: string, value: string): void {
    const stmt = this.db.prepare(`
      INSERT OR REPLACE INTO state (key, value, updatedAt)
      VALUES (?, ?, ?)
    `);
    stmt.run(key, value, new Date().toISOString());
  }

  getState(key: string): string | undefined {
    const stmt = this.db.prepare('SELECT value FROM state WHERE key = ?');
    const result = stmt.get(key) as { value: string } | undefined;
    return result?.value;
  }

  // === Cleanup ===

  close(): void {
    this.db.close();
  }
}

/**
 * Create store in bot's logs directory.
 */
export function createStore(botName: string): StateStore {
  const dbPath = path.join(__dirname, '../../logs', `${botName}-state.db`);
  return new StateStore(dbPath);
}
