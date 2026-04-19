/**
 * VALR WebSocket Account Client
 * 
 * Subscribes to account updates for order fills, balance changes.
 */

import WebSocket from 'ws';
import crypto from 'crypto';
import { createLogger } from '../app/logger.js';

const log = createLogger('wsAccount');

export interface AccountUpdate {
  type: 'order_update' | 'balance_update' | 'position_update';
  data: any;
  timestamp: number;
}

export type AccountHandler = (update: AccountUpdate) => void;

export interface WSAccountClientOptions {
  apiKey: string;
  apiSecret: string;
  subaccountId?: string;
  onUpdate: AccountHandler;
}

export class WSAccountClient {
  private ws: WebSocket | null = null;
  private apiKey: string;
  private apiSecret: string;
  private subaccountId?: string;
  private onUpdate: AccountHandler;
  private reconnectDelayMs = 1000;
  private maxReconnectDelayMs = 30000;

  constructor(options: WSAccountClientOptions) {
    this.apiKey = options.apiKey;
    this.apiSecret = options.apiSecret;
    this.subaccountId = options.subaccountId;
    this.onUpdate = options.onUpdate;
  }

  private sign(timestamp: string): string {
    const message = `${timestamp}`;
    return crypto.createHmac('sha512', this.apiSecret).update(message).digest('hex');
  }

  connect(): void {
    if (this.ws) {
      this.ws.close();
    }

    const url = 'wss://ws.valr.com';
    log.info({ url }, 'Connecting to VALR Account WebSocket');

    this.ws = new WebSocket(url);

    this.ws.on('open', () => {
      log.info('Account WebSocket connected');
      this.reconnectDelayMs = 1000;
      this.authenticate();
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
      log.error({ err }, 'Account WebSocket error');
    });

    this.ws.on('close', () => {
      log.warn('Account WebSocket closed');
      this.scheduleReconnect();
    });
  }

  private authenticate(): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;

    const timestamp = Date.now().toString();
    const signature = this.sign(timestamp);

    const authMsg = {
      type: 'authenticate',
      apiKey: this.apiKey,
      timestamp,
      signature,
      subaccountId: this.subaccountId,
    };

    this.ws.send(JSON.stringify(authMsg));
    log.info('Authentication sent');
  }

  private handleMessage(msg: any): void {
    if (msg.type === 'authentication_success') {
      log.info('Authenticated');
      this.subscribe();
      return;
    }

    if (msg.type === 'authentication_failure') {
      log.error({ reason: msg.reason }, 'Authentication failed');
      return;
    }

    if (msg.type === 'order_update') {
      this.onUpdate({
        type: 'order_update',
        data: msg,
        timestamp: Date.now(),
      });
    }

    if (msg.type === 'balance_update') {
      this.onUpdate({
        type: 'balance_update',
        data: msg,
        timestamp: Date.now(),
      });
    }
  }

  private subscribe(): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;

    // Subscribe to account events
    const subMsg = {
      type: 'subscribe',
      channel: 'account',
    };
    this.ws.send(JSON.stringify(subMsg));
    log.info('Subscribed to account updates');
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
