/**
 * VALR Account WebSocket Client
 * wss://api.valr.com/ws/account
 *
 * Auth: send X-VALR-API-KEY, X-VALR-SIGNATURE, X-VALR-TIMESTAMP headers on connect
 * WS signature: HMAC-SHA512(secret, timestamp + "GET" + "/ws/account")  — no body, no subaccountId
 *
 * Auto-pushed events (no subscribe needed):
 *   BALANCE_UPDATE, OPEN_ORDERS_UPDATE, ORDER_PROCESSED, FAILED_ORDER, FAILED_CANCEL_ORDER
 *
 * Must subscribe explicitly:
 *   ORDER_STATUS_UPDATE
 *
 * Also consume (auto-pushed for futures accounts):
 *   OPEN_POSITION_UPDATE, REDUCE_POSITION, POSITION_CLOSED
 *   ADD_CONDITIONAL_ORDER, REMOVE_CONDITIONAL_ORDER
 */

import WebSocket from 'ws';
import { buildWsAuthHeaders } from './auth.js';
import type {
  WsEnvelope,
  WsOrderStatusUpdate,
  WsOpenPositionUpdate,
  WsPositionClosed,
  WsFailedOrder,
} from './types.js';
import { createLogger } from '../app/logger.js';

const log = createLogger('wsAccount');

const WS_URL = 'wss://api.valr.com/ws/account';
const PING_INTERVAL_MS = 20_000;
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

export type AccountEventHandler = {
  onOrderStatusUpdate?: (data: WsOrderStatusUpdate) => void;
  onOpenPositionUpdate?: (data: WsOpenPositionUpdate) => void;
  onPositionClosed?: (data: WsPositionClosed) => void;
  onFailedOrder?: (data: WsFailedOrder) => void;
  onFailedCancelOrder?: (data: WsFailedOrder) => void;
  onRawMessage?: (type: string, data: unknown) => void;
  onConnected?: () => void;
  onDisconnected?: () => void;
};

export class WsAccountClient {
  private apiKey: string;
  private apiSecret: string;
  private handlers: AccountEventHandler;
  private ws: WebSocket | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectDelay = RECONNECT_BASE_MS;
  private staleTimer: ReturnType<typeof setTimeout> | null = null;
  private staleTimeoutMs: number;
  private stopped = false;
  private lastMessageAt = 0;

  constructor(
    apiKey: string,
    apiSecret: string,
    handlers: AccountEventHandler,
    staleTimeoutMs = 30_000
  ) {
    this.apiKey = apiKey;
    this.apiSecret = apiSecret;
    this.handlers = handlers;
    this.staleTimeoutMs = staleTimeoutMs;
  }

  connect(): void {
    this.stopped = false;
    this._connect();
  }

  stop(): void {
    this.stopped = true;
    this._clearTimers();
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  sendCommand(type: string, clientMsgId: string, payload: Record<string, unknown>): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      log.warn({ type }, 'WS not connected — cannot send command');
      return;
    }
    const msg = JSON.stringify({ type, clientMsgId, payload });
    this.ws.send(msg);
  }

  private _connect(): void {
    if (this.stopped) return;

    const authHeaders = buildWsAuthHeaders(this.apiKey, this.apiSecret);
    log.info('Connecting to account WebSocket');

    this.ws = new WebSocket(WS_URL, { headers: authHeaders });

    this.ws.on('open', () => {
      log.info('Account WebSocket connected');
      this.reconnectDelay = RECONNECT_BASE_MS;
      this.lastMessageAt = Date.now();

      // Subscribe to ORDER_STATUS_UPDATE (must be explicit)
      this.ws!.send(
        JSON.stringify({
          type: 'SUBSCRIBE',
          subscriptions: [{ event: 'ORDER_STATUS_UPDATE' }],
        })
      );

      // Start ping to keep connection alive
      this._startPing();
      this._startStaleCheck();
      this.handlers.onConnected?.();
    });

    this.ws.on('message', (raw) => {
      this.lastMessageAt = Date.now();
      try {
        const envelope = JSON.parse(raw.toString()) as WsEnvelope;
        this._dispatch(envelope);
      } catch (err) {
        log.error({ err, raw: raw.toString() }, 'Failed to parse WS message');
      }
    });

    this.ws.on('error', (err) => {
      log.error({ err }, 'Account WebSocket error');
    });

    this.ws.on('close', (code, reason) => {
      log.warn({ code, reason: reason.toString() }, 'Account WebSocket closed');
      this._clearTimers();
      this.handlers.onDisconnected?.();
      if (!this.stopped) {
        this._scheduleReconnect();
      }
    });
  }

  private _dispatch(envelope: WsEnvelope): void {
    const { type, data } = envelope;

    this.handlers.onRawMessage?.(type, data);

    switch (type) {
      case 'ORDER_STATUS_UPDATE':
        this.handlers.onOrderStatusUpdate?.(data as WsOrderStatusUpdate);
        break;
      case 'OPEN_POSITION_UPDATE':
        this.handlers.onOpenPositionUpdate?.(data as WsOpenPositionUpdate);
        break;
      case 'POSITION_CLOSED':
        this.handlers.onPositionClosed?.(data as WsPositionClosed);
        break;
      case 'REDUCE_POSITION':
        // Treat as position update — refetch via REST
        this.handlers.onRawMessage?.('REDUCE_POSITION', data);
        break;
      case 'FAILED_ORDER':
        this.handlers.onFailedOrder?.(data as WsFailedOrder);
        break;
      case 'FAILED_CANCEL_ORDER':
        this.handlers.onFailedCancelOrder?.(data as WsFailedOrder);
        break;
      case 'AUTHENTICATED':
        log.info('Account WebSocket authenticated');
        break;
      case 'PLACE_LIMIT_WS_RESPONSE':
        log.debug({ data }, 'WS limit order response');
        this.handlers.onRawMessage?.('PLACE_LIMIT_WS_RESPONSE', data);
        break;
      case 'INVALID_PLACE_LIMIT_REQUEST':
        log.warn({ data }, 'WS limit order rejected');
        this.handlers.onRawMessage?.('INVALID_PLACE_LIMIT_REQUEST', data);
        break;
      case 'BALANCE_UPDATE':
      case 'OPEN_ORDERS_UPDATE':
      case 'ORDER_PROCESSED':
      case 'NEW_ACCOUNT_TRADE':
      case 'NEW_ACCOUNT_HISTORY_RECORD':
      case 'ADD_CONDITIONAL_ORDER':
      case 'REMOVE_CONDITIONAL_ORDER':
        // Pass through for interested callers
        this.handlers.onRawMessage?.(type, data);
        break;
      default:
        log.debug({ type, data }, 'Unhandled WS account event');
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
    // Account WS only emits on order/position events — use a generous stale window
    // (5 min) to avoid thrashing. Ping every 20s keeps the connection alive.
    const staleMs = Math.max(this.staleTimeoutMs, 300_000);
    this.staleTimer = setInterval(() => {
      const age = Date.now() - this.lastMessageAt;
      if (age > staleMs) {
        log.warn({ ageSecs: Math.round(age / 1000) }, 'Account WS stale — forcing reconnect');
        this.ws?.terminate();
      }
    }, 60_000);
  }

  private _scheduleReconnect(): void {
    if (this.stopped) return;
    log.info({ delayMs: this.reconnectDelay }, 'Scheduling account WS reconnect');
    this.reconnectTimer = setTimeout(() => {
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, RECONNECT_MAX_MS);
      this._connect();
    }, this.reconnectDelay);
  }

  private _clearPing(): void {
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }

  private _clearStale(): void {
    if (this.staleTimer) {
      clearInterval(this.staleTimer);
      this.staleTimer = null;
    }
  }

  private _clearTimers(): void {
    this._clearPing();
    this._clearStale();
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }
}
