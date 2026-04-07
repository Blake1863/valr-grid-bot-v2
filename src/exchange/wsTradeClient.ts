/**
 * VALR Trade WebSocket Client (public, no auth)
 * wss://api.valr.com/ws/trade
 *
 * Subscribes to:
 * - AGGREGATED_ORDERBOOK_UPDATE (for mid price)
 * - MARK_PRICE_UPDATE (for futures mark price)
 * - MARKET_SUMMARY_UPDATE (fallback)
 */

import WebSocket from 'ws';
import Decimal from 'decimal.js';
import type {
  WsEnvelope,
  WsAggregatedOrderbookUpdate,
  WsMarkPriceUpdate,
  WsMarketSummaryUpdate,
} from './types.js';
import { createLogger } from '../app/logger.js';

const log = createLogger('wsTrade');

const WS_URL = 'wss://api.valr.com/ws/trade';
const PING_INTERVAL_MS = 20_000;
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

export class WsTradeClient {
  private pair: string;
  private ws: WebSocket | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private staleTimer: ReturnType<typeof setInterval> | null = null;
  private reconnectDelay = RECONNECT_BASE_MS;
  private stopped = false;
  private lastMessageAt = 0;
  private staleTimeoutMs: number;

  // Latest prices
  private _markPrice: Decimal | null = null;
  private _midPrice: Decimal | null = null;
  private _lastTradedPrice: Decimal | null = null;

  constructor(pair: string, staleTimeoutMs = 30_000) {
    this.pair = pair;
    this.staleTimeoutMs = staleTimeoutMs;
  }

  get markPrice(): Decimal | null {
    return this._markPrice;
  }

  get midPrice(): Decimal | null {
    return this._midPrice;
  }

  get lastTradedPrice(): Decimal | null {
    return this._lastTradedPrice;
  }

  /** Best available price: mark > mid > last traded */
  get bestPrice(): Decimal | null {
    return this._markPrice ?? this._midPrice ?? this._lastTradedPrice ?? null;
  }

  connect(): void {
    this.stopped = false;
    this._connect();
  }

  stop(): void {
    this.stopped = true;
    this._clearTimers();
    this.ws?.close();
    this.ws = null;
  }

  private _connect(): void {
    if (this.stopped) return;
    log.info({ pair: this.pair }, 'Connecting to trade WebSocket');

    this.ws = new WebSocket(WS_URL);

    this.ws.on('open', () => {
      log.info({ pair: this.pair }, 'Trade WebSocket connected');
      this.reconnectDelay = RECONNECT_BASE_MS;
      this.lastMessageAt = Date.now();

      // Subscribe to all feeds we need
      this.ws!.send(
        JSON.stringify({
          type: 'SUBSCRIBE',
          subscriptions: [
            { event: 'AGGREGATED_ORDERBOOK_UPDATE', pairs: [this.pair] },
            { event: 'MARK_PRICE_UPDATE', pairs: [this.pair] },
            { event: 'MARKET_SUMMARY_UPDATE', pairs: [this.pair] },
          ],
        })
      );

      this._startPing();
      this._startStaleCheck();
    });

    this.ws.on('message', (raw) => {
      this.lastMessageAt = Date.now();
      try {
        const envelope = JSON.parse(raw.toString()) as WsEnvelope;
        this._dispatch(envelope);
      } catch (err) {
        log.error({ err }, 'Failed to parse trade WS message');
      }
    });

    this.ws.on('error', (err) => {
      log.error({ err }, 'Trade WebSocket error');
    });

    this.ws.on('close', (code) => {
      log.warn({ code }, 'Trade WebSocket closed');
      this._clearTimers();
      if (!this.stopped) {
        this._scheduleReconnect();
      }
    });
  }

  private _dispatch(envelope: WsEnvelope): void {
    const { type, data } = envelope;

    switch (type) {
      case 'AGGREGATED_ORDERBOOK_UPDATE': {
        const ob = data as WsAggregatedOrderbookUpdate;
        const bestAsk = ob.Asks?.[0];
        const bestBid = ob.Bids?.[0];
        if (bestAsk && bestBid) {
          const ask = new Decimal(bestAsk.price);
          const bid = new Decimal(bestBid.price);
          this._midPrice = ask.add(bid).div(2);
        }
        break;
      }
      case 'MARK_PRICE_UPDATE': {
        const mp = data as WsMarkPriceUpdate;
        if (mp.markPrice) {
          this._markPrice = new Decimal(mp.markPrice);
        }
        break;
      }
      case 'MARKET_SUMMARY_UPDATE': {
        const ms = data as WsMarketSummaryUpdate;
        if (ms.markPrice) {
          this._markPrice = new Decimal(ms.markPrice);
        }
        if (ms.lastTradedPrice) {
          this._lastTradedPrice = new Decimal(ms.lastTradedPrice);
        }
        if (ms.bidPrice && ms.askPrice) {
          const bid = new Decimal(ms.bidPrice);
          const ask = new Decimal(ms.askPrice);
          if (this._midPrice === null) {
            this._midPrice = bid.add(ask).div(2);
          }
        }
        break;
      }
      default:
        // Ignore other events (trade stream has many)
        break;
    }
  }

  private _startPing(): void {
    this._clearPing();
    this.pingTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.ping();
      }
    }, PING_INTERVAL_MS);
  }

  private _startStaleCheck(): void {
    this._clearStale();
    this.staleTimer = setInterval(() => {
      const age = Date.now() - this.lastMessageAt;
      if (age > this.staleTimeoutMs) {
        log.warn({ ageSecs: Math.round(age / 1000) }, 'Trade WS stale — forcing reconnect');
        this.ws?.terminate();
      }
    }, this.staleTimeoutMs / 2);
  }

  private _scheduleReconnect(): void {
    if (this.stopped) return;
    log.info({ delayMs: this.reconnectDelay }, 'Scheduling trade WS reconnect');
    this.reconnectTimer = setTimeout(() => {
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, RECONNECT_MAX_MS);
      this._connect();
    }, this.reconnectDelay);
  }

  private _clearPing(): void {
    if (this.pingTimer) { clearInterval(this.pingTimer); this.pingTimer = null; }
  }

  private _clearStale(): void {
    if (this.staleTimer) { clearInterval(this.staleTimer); this.staleTimer = null; }
  }

  private _clearTimers(): void {
    this._clearPing();
    this._clearStale();
    if (this.reconnectTimer) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null; }
  }
}
