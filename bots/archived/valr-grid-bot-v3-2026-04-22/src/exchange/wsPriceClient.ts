/**
 * VALR WebSocket Price Client
 * 
 * Subscribes to mark price / orderbook updates for real-time grid management.
 * Uses wss://api.valr.com/ws/trade (public, no auth required)
 */

import WebSocket from 'ws';
import { Decimal } from 'decimal.js';
import { createLogger } from '../app/logger.js';

const log = createLogger('wsPrice');

const WS_URL = 'wss://api.valr.com/ws/trade';

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
  private pingTimer: NodeJS.Timeout | null = null;

  constructor(options: WSPriceClientOptions) {
    this.pairs = options.pairs;
    this.onPrice = options.onPrice;
    this.staleTimeoutMs = options.staleTimeoutMs ?? 30000;
  }

  connect(): void {
    if (this.ws) {
      this.ws.close();
    }

    log.info({ url: WS_URL, pairs: this.pairs }, 'Connecting to VALR trade WebSocket');

    this.ws = new WebSocket(WS_URL);

    this.ws.on('open', () => {
      log.info('WebSocket connected');
      this.reconnectDelayMs = 1000;
      this.lastUpdate.clear();
      this.subscribe();
      this.startPing();
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

    this.ws.on('close', (code) => {
      log.warn({ code }, 'WebSocket closed');
      this.healthy = false;
      this.stopPing();
      this.scheduleReconnect();
    });

    // Health check
    setInterval(() => this.checkHealth(), 5000);
  }

  private subscribe(): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;

    // Subscribe to mark price and orderbook updates
    const subMsg = {
      type: 'SUBSCRIBE',
      subscriptions: [
        { event: 'MARK_PRICE_UPDATE', pairs: this.pairs },
        { event: 'AGGREGATED_ORDERBOOK_UPDATE', pairs: this.pairs },
        { event: 'MARKET_SUMMARY_UPDATE', pairs: this.pairs },
      ],
    };
    
    this.ws.send(JSON.stringify(subMsg));
    log.info({ pairs: this.pairs }, 'Subscribed to price feeds');
  }

  private handleMessage(msg: any): void {
    const type = msg.type;
    const data = msg.data || msg;
    const pair = data?.currencyPairSymbol || data?.currencyPair || data?.pair;

    if (!pair) return;

    let markPrice: Decimal | null = null;
    let lastPrice: Decimal | null = null;

    if (type === 'MARK_PRICE_UPDATE') {
      markPrice = new Decimal(data.markPrice || '0');
      lastPrice = markPrice;
    } else if (type === 'AGGREGATED_ORDERBOOK_UPDATE') {
      const bestBid = data.Bids?.[0];
      const bestAsk = data.Asks?.[0];
      if (bestBid && bestAsk) {
        const bid = new Decimal(bestBid.price);
        const ask = new Decimal(bestAsk.price);
        lastPrice = bid.add(ask).div(2);
        markPrice = lastPrice;
      }
    } else if (type === 'MARKET_SUMMARY_UPDATE') {
      lastPrice = new Decimal(data.lastTradedPrice || '0');
      markPrice = new Decimal(data.markPrice || data.lastTradedPrice || '0');
    }

    if (markPrice) {
      this.lastUpdate.set(pair, Date.now());
      
      this.onPrice({
        pair,
        markPrice,
        lastPrice: lastPrice || markPrice,
        timestamp: Date.now(),
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

  private startPing(): void {
    this.pingTimer = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.ping();
      }
    }, 20000);
  }

  private stopPing(): void {
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }

  private scheduleReconnect(): void {
    log.info({ delayMs: this.reconnectDelayMs }, 'Scheduling reconnect');
    
    setTimeout(() => {
      this.reconnectDelayMs = Math.min(this.reconnectDelayMs * 2, this.maxReconnectDelayMs);
      this.connect();
    }, this.reconnectDelayMs);
  }

  disconnect(): void {
    this.stopPing();
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}
