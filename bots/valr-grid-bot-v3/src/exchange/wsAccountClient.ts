/**
 * VALR Account Update Poller
 * 
 * Polls for order updates since VALR doesn't have a public account WebSocket.
 * This is used for detecting fills and triggering grid cycle completion.
 */

import { createLogger } from '../app/logger.js';

const log = createLogger('wsAccount');

export interface AccountUpdate {
  type: 'order_update' | 'balance_update' | 'position_update';
  data: any;
  timestamp: number;
}

export type AccountHandler = (update: AccountUpdate) => void;

export interface WSAccountClientOptions {
  pollIntervalMs?: number;
  onUpdate: AccountHandler;
  fetchOpenOrders: () => Promise<any[]>;
}

export class WSAccountClient {
  private pollIntervalMs: number;
  private onUpdate: AccountHandler;
  private fetchOpenOrders: () => Promise<any[]>;
  private timer: NodeJS.Timeout | null = null;
  private lastOrderState: Map<string, string> = new Map(); // orderId -> status

  constructor(options: WSAccountClientOptions) {
    this.pollIntervalMs = options.pollIntervalMs ?? 5000;
    this.onUpdate = options.onUpdate;
    this.fetchOpenOrders = options.fetchOpenOrders;
  }

  start(): void {
    log.info({ intervalMs: this.pollIntervalMs }, 'Starting account poller');
    this.poll();
    this.timer = setInterval(() => this.poll(), this.pollIntervalMs);
  }

  private async poll(): Promise<void> {
    try {
      const orders = await this.fetchOpenOrders();
      
      // Check for status changes
      for (const order of orders) {
        const prevStatus = this.lastOrderState.get(order.orderId);
        const currStatus = order.status;
        
        if (prevStatus && prevStatus !== currStatus) {
          log.info({ orderId: order.orderId, prevStatus, currStatus }, 'Order status changed');
          
          if (currStatus === 'FILLED' || currStatus === 'PARTIALLY_FILLED') {
            this.onUpdate({
              type: 'order_update',
              data: order,
              timestamp: Date.now(),
            });
          }
        }
        
        this.lastOrderState.set(order.orderId, currStatus);
      }

      // Clean up filled orders from tracking
      const currentIds = new Set(orders.map(o => o.orderId));
      for (const [orderId] of this.lastOrderState) {
        if (!currentIds.has(orderId)) {
          this.lastOrderState.delete(orderId);
        }
      }
    } catch (err: any) {
      log.warn({ err: err.message }, 'Poll failed');
    }
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}
