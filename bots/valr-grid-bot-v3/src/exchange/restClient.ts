/**
 * VALR REST Client — Perpetual Futures
 */

import crypto from 'crypto';
import https from 'https';
import type { ValrOrder, ValrPosition, ValrBalance, ValrTicker, ValrMarkPrice, OrderPlacement } from './types.js';

const BASE_URL = 'https://api.valr.com';

export interface ValrRestClientOptions {
  apiKey: string;
  apiSecret: string;
  subaccountId?: string;
  dryRun?: boolean;
}

interface HttpResponse {
  statusCode: number;
  data: string;
}

function httpsGet(url: string, headers: Record<string, string>): Promise<HttpResponse> {
  return new Promise((resolve, reject) => {
    https.get(url, { headers }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => resolve({ statusCode: res.statusCode || 0, data }));
    }).on('error', reject);
  });
}

function httpsRequest(method: string, url: string, headers: Record<string, string>, body?: string): Promise<HttpResponse> {
  return new Promise((resolve, reject) => {
    const urlObj = new URL(url);
    const options = {
      hostname: urlObj.hostname,
      port: 443,
      path: urlObj.pathname + urlObj.search,
      method,
      headers,
    };
    
    const req = https.request(options, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => resolve({ statusCode: res.statusCode || 0, data }));
    });
    
    req.on('error', reject);
    
    if (body) {
      req.write(body);
    }
    req.end();
  });
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
    
    // Debug logging for POST requests
    if (method === 'POST') {
      console.log(`[DEBUG] POST ${path} body:`, bodyStr);
    }
    
    const headers = this.getHeaders(method, path, bodyStr);

    if (this.dryRun && method !== 'GET') {
      console.log(`[DRY RUN] ${method} ${path}`, body);
      return {} as T;
    }

    const res = method === 'GET' 
      ? await httpsGet(url, headers)
      : await httpsRequest(method, url, headers, bodyStr);

    if (res.statusCode < 200 || res.statusCode >= 300) {
      throw new Error(`[REST ${method} ${path}] HTTP ${res.statusCode}: ${res.data}`);
    }

    return JSON.parse(res.data) as T;
  }

  // === Market Data ===

  async getTicker(pair: string): Promise<ValrTicker> {
    // Use public market summary endpoint
    const currencyPair = pair.replace('PERP', ''); // SOLUSDTPERP -> SOLUSDT
    const res = await this.request<any>('GET', `/v1/public/${currencyPair}/marketsummary`);
    return {
      pair,
      bid: res.bidPrice || '0',
      ask: res.askPrice || '0',
      last: res.lastTradedPrice || '0',
      high: res.highPrice || '0',
      low: res.lowPrice || '0',
      volume: res.baseVolume || '0',
      timestamp: res.created || new Date().toISOString(),
    };
  }

  async getMarkPrice(pair: string): Promise<ValrMarkPrice> {
    const ticker = await this.getTicker(pair);
    return {
      pair,
      markPrice: ticker.last,
      indexPrice: ticker.last,
      timestamp: ticker.timestamp,
    };
  }

  // === Orders ===

  async getOpenOrders(): Promise<ValrOrder[]> {
    return this.request<ValrOrder[]>('GET', '/v1/orders/open');
  }

  async placeLimitOrder(order: OrderPlacement): Promise<ValrOrder> {
    return this.request<ValrOrder>('POST', '/v1/orders/limit', order);
  }

  async cancelOrder(orderId: string): Promise<void> {
    await this.request('DELETE', `/v1/orders/${orderId}`);
  }

  async cancelAllOrders(pair: string): Promise<void> {
    // Cancel all open orders for the subaccount
    const orders = await this.getOpenOrders();
    for (const order of orders) {
      const orderId = order.orderId || order.id;
      if (order.pair === pair && orderId) {
        try {
          await this.cancelOrder(orderId);
        } catch (err) {
          // Ignore errors
        }
      }
    }
  }

  // === Positions ===

  async getPositions(): Promise<ValrPosition[]> {
    try {
      return await this.request<ValrPosition[]>('GET', '/v1/positions/open');
    } catch (err) {
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
      return await this.request<ValrBalance[]>('GET', '/v1/account/balances');
    } catch (err) {
      return [];
    }
  }

  async getBalance(asset: string): Promise<ValrBalance | null> {
    const balances = await this.getBalances();
    return balances.find(b => b.asset === asset) || null;
  }
}
