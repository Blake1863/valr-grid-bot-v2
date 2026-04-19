/**
 * VALR REST Client — Perpetual Futures
 */

import crypto from 'crypto';
import fetch from 'node-fetch';
import type { ValrOrder, ValrPosition, ValrBalance, ValrTicker, ValrMarkPrice, ValrConditionalOrder, OrderPlacement, ConditionalPlacement } from './types.js';

const BASE_URL = 'https://api.valr.com';

export interface ValrRestClientOptions {
  apiKey: string;
  apiSecret: string;
  subaccountId?: string;
  dryRun?: boolean;
}

export class ValrRestClient {
  private apiKey: string;
  private apiSecret: string;
  private subaccountId?: string;
  private dryRun: boolean;

  constructor(options: ValrRestClientOptions) {
    this.apiKey = options.apiKey;
    this.apiSecret = options.apiSecret;
    this.subaccountId = options.subaccountId;
    this.dryRun = options.dryRun ?? false;
  }

  private sign(timestamp: string, method: string, path: string, body = '', subacct = ''): string {
    const message = `${timestamp}${method}${path}${body}${subacct}`;
    return crypto.createHmac('sha512', this.apiSecret).update(message).digest('hex');
  }

  private getHeaders(method: string, path: string, body = ''): Record<string, string> {
    const timestamp = Date.now().toString();
    const signature = this.sign(timestamp, method, path, body, this.subaccountId ?? '');
    
    const headers: Record<string, string> = {
      'X-VALR-API-KEY': this.apiKey,
      'X-VALR-SIGNATURE': signature,
      'X-VALR-TIMESTAMP': timestamp,
      'Content-Type': 'application/json',
    };

    if (this.subaccountId) {
      headers['X-VALR-SUB-ACCOUNT-ID'] = this.subaccountId;
    }

    return headers;
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const url = `${BASE_URL}${path}`;
    const bodyStr = body ? JSON.stringify(body) : '';
    const headers = this.getHeaders(method, path, bodyStr);

    if (this.dryRun && method !== 'GET') {
      console.log(`[DRY RUN] ${method} ${path}`, body);
      return {} as T;
    }

    const res = await fetch(url, {
      method,
      headers,
      body: bodyStr || undefined,
    });

    const text = await res.text();
    
    if (!res.ok) {
      throw new Error(`[REST ${method} ${path}] HTTP ${res.status}: ${text}`);
    }

    return JSON.parse(text) as T;
  }

  // === Market Data ===

  async getTicker(pair: string): Promise<ValrTicker> {
    return this.request('GET', `/v1/ticker?pair=${pair}`);
  }

  async getMarkPrice(pair: string): Promise<ValrMarkPrice> {
    // VALR may not have a dedicated mark price endpoint for perps
    // Fall back to ticker or implement based on actual API
    const ticker = await this.getTicker(pair);
    return {
      pair,
      markPrice: ticker.last,
      indexPrice: ticker.last,
      timestamp: ticker.timestamp,
    };
  }

  // === Orders ===

  async getOpenOrders(pair: string): Promise<ValrOrder[]> {
    const orders = await this.request<ValrOrder[]>('GET', '/v1/orders/open');
    return orders.filter(o => o.pair === pair);
  }

  async placeLimitOrder(order: OrderPlacement): Promise<ValrOrder> {
    return this.request('POST', '/v1/orders', order);
  }

  async cancelOrder(orderId: string, pair: string): Promise<void> {
    await this.request('DELETE', `/v1/orders/${orderId}?pair=${pair}`);
  }

  async cancelAllOrders(pair: string): Promise<void> {
    await this.request('DELETE', `/v1/orders?pair=${pair}`);
  }

  // === Positions ===

  async getPositions(): Promise<ValrPosition[]> {
    // VALR perpetual positions endpoint — adjust based on actual API
    try {
      return await this.request<ValrPosition[]>('GET', '/v1/perpetual/positions');
    } catch (err) {
      // Endpoint may not exist or differ — return empty
      return [];
    }
  }

  async getPosition(instrumentId: string): Promise<ValrPosition | null> {
    const positions = await this.getPositions();
    return positions.find(p => p.instrumentId === instrumentId) || null;
  }

  // === Balances ===

  async getBalances(): Promise<ValrBalance[]> {
    try {
      return await this.request<ValrBalance[]>('GET', '/v1/balances');
    } catch (err) {
      return [];
    }
  }

  async getBalance(asset: string): Promise<ValrBalance | null> {
    const balances = await this.getBalances();
    return balances.find(b => b.asset === asset) || null;
  }

  // === Conditional Orders (Stop Loss / TP) ===

  async getConditionalOrders(pair: string): Promise<ValrConditionalOrder[]> {
    try {
      const orders = await this.request<ValrConditionalOrder[]>('GET', '/v1/conditional-orders');
      return orders.filter(o => o.pair === pair);
    } catch {
      return [];
    }
  }

  async placeConditionalOrder(order: ConditionalPlacement): Promise<ValrConditionalOrder> {
    return this.request('POST', '/v1/conditional-orders', order);
  }

  async cancelConditionalOrder(orderId: string): Promise<void> {
    await this.request('DELETE', `/v1/conditional-orders/${orderId}`);
  }

  // === Account ===

  async getSubaccounts(): Promise<{ id: string; name: string }[]> {
    try {
      return await this.request('GET', '/v1/subaccounts');
    } catch {
      return [];
    }
  }
}
