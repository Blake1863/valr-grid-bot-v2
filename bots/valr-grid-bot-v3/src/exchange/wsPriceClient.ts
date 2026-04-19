/**
 * VALR WebSocket Price Client
 * 
 * Subscribes to mark price / ticker updates for real-time grid management.
 */

import WebSocket from 'ws';
import { createLogger } from '../app/logger.js';
import { Decimal } from 'decimal.js';

const log = createLogger('wsPrice');

export interface PriceUpdate {
  pair: string;
  markPrice: Decimal;
  lastPrice: Decimal;
  timestamp: number;
}

export type PriceHandler = (update: PriceUpdate) => void;

export interface WSPriceClientOptions {
  pairs: string[];
  onPrice: PriceHandler;
  staleTimeoutMs?: number;
}

export class WSPriceClient {
  private ws: WebSocket | null = null;
  private pairs: string[];
  private onPrice: PriceHandler;
  private staleTimeoutMs: number;
  private lastUpdate: Map<string, number> = new Map();
  private reconnectDelayMs = 1000;
  private maxReconnectDelayMs = 30000;
  private healthy: boolean = false;

  constructor(options: WSPriceClientOptions) {
    this.pairs = options.pairs;
    this.onPrice = options.onPrice;
    this.staleTimeoutMs = options.staleTimeoutMs ?? 30000;
  }

  connect(): void {
    if (this.ws) {
      this.ws.close();
    }

    const url = 'wss://ws.valr.com';
    log.info({ url }, 'Connecting to VALR WebSocket');

    this.ws = new WebSocket(url);

    this.ws.on('open', () => {
      log.info('WebSocket connected');
      this.reconnectDelayMs = 1000;
      this.subscribe();
    });

    this.ws.on('message', (data: WebSocket.Data) => {
      try {
        const msg = JSON.parse(data.toString());
        this.handleMessage(msg);
      } catch (err) {
        log.warn({ err, raw: data.toString().slice(0, 200) }, 'Failed to parse WS message');
      }
    });

    this.ws.on('error', (err: Error) => {
      log.error({ err }, 'WebSocket error');
      this.healthy = false;
    });

    this.ws.on('close', () => {
      log.warn('WebSocket closed');
      this.healthy = false;
      this.scheduleReconnect();
    });

    // Health check
    setInterval(() => this.checkHealth(), 5000);
  }

  private subscribe(): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;

    // Subscribe to ticker for each pair
    for (const pair of this.pairs) {
      const subMsg = {
        type: 'subscribe',
        channel: 'ticker',
        pair,
      };
      this.ws.send(JSON.stringify(subMsg));
      log.info({ pair }, 'Subscribed to ticker');
    }
  }

  private handleMessage(msg: any): void {
    if (msg.type === 'ticker') {
      const pair = msg.pair;
      const markPrice = new Decimal(msg.last || msg.markPrice || '0');
      const lastPrice = new Decimal(msg.last || '0');
      const timestamp = Date.now();

      this.lastUpdate.set(pair, timestamp);

      this.onPrice({
        pair,
        markPrice,
        lastPrice,
        timestamp,
      });
    }
  }

  private checkHealth(): void {
    const now = Date.now();
    let allHealthy = true;

    for (const pair of this.pairs) {
      const lastUpdate = this.lastUpdate.get(pair) || 0;
      const age = now - lastUpdate;
      
      if (age > this.staleTimeoutMs) {
        log.warn({ pair, ageMs: age }, 'Price data stale');
        allHealthy = false;
      }
    }

    this.healthy = allHealthy;
  }

  isHealthy(): boolean {
    return this.healthy;
  }

  private scheduleReconnect(): void {
    log.info({ delayMs: this.reconnectDelayMs }, 'Scheduling reconnect');
    
    setTimeout(() => {
      this.reconnectDelayMs = Math.min(this.reconnectDelayMs * 2, this.maxReconnectDelayMs);
      this.connect();
    }, this.reconnectDelayMs);
  }

  disconnect(): void {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}
