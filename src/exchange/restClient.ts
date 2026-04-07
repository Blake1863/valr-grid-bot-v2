/**
 * VALR REST Client
 * All price/qty strings passed as-is (caller must round/truncate before calling)
 */

import axios, { AxiosInstance, AxiosError } from 'axios';
import { buildAuthHeaders } from './auth.js';
import type {
  ValrPairInfo,
  ValrOpenPosition,
  PlaceLimitOrderRequest,
  PlaceMarketOrderRequest,
  OrderAcceptedResponse,
  PlaceConditionalOrderRequest,
  ConditionalOrderResponse,
  ValrConditionalOrder,
  BatchOrdersPayload,
  BatchOrdersResponse,
  ValrMarketSummary,
  ValrBalance,
  ValrOpenOrder,
  ValrErrorResponse,
} from './types.js';
import { createLogger } from '../app/logger.js';

const log = createLogger('restClient');

const BASE_URL = 'https://api.valr.com';

export class ValrRestClient {
  private http: AxiosInstance;
  private apiKey: string;
  private apiSecret: string;
  private subaccountId: string;

  constructor(apiKey: string, apiSecret: string, subaccountId: string = '') {
    this.apiKey = apiKey;
    this.apiSecret = apiSecret;
    this.subaccountId = subaccountId;

    this.http = axios.create({
      baseURL: BASE_URL,
      timeout: 15000,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // ─── Auth helpers ─────────────────────────────────────────────────────────

  private authHeaders(verb: string, path: string, body: string = '') {
    return buildAuthHeaders(this.apiKey, this.apiSecret, verb, path, body, this.subaccountId);
  }

  private handleError(err: unknown, context: string): never {
    if (err instanceof AxiosError) {
      const status = err.response?.status;
      const data = err.response?.data as ValrErrorResponse | undefined;
      const msg = data?.message ?? err.message;
      throw new Error(`[${context}] HTTP ${status}: ${msg} (code: ${data?.code ?? 'N/A'})`);
    }
    throw err;
  }

  // ─── Public endpoints ─────────────────────────────────────────────────────

  async getPairsByType(type: 'FUTURE' | 'SPOT' = 'FUTURE'): Promise<ValrPairInfo[]> {
    try {
      const path = `/v1/public/pairs/${type}`;
      const res = await this.http.get<ValrPairInfo[]>(path);
      return res.data;
    } catch (err) {
      this.handleError(err, 'getPairsByType');
    }
  }

  async getMarketSummary(currencyPair: string): Promise<ValrMarketSummary> {
    try {
      const path = `/v1/public/${currencyPair}/marketsummary`;
      const res = await this.http.get<ValrMarketSummary>(path);
      return res.data;
    } catch (err) {
      this.handleError(err, 'getMarketSummary');
    }
  }

  // ─── Account endpoints ────────────────────────────────────────────────────

  async getBalances(): Promise<ValrBalance[]> {
    const path = '/v1/account/balances';
    try {
      const headers = this.authHeaders('GET', path);
      const res = await this.http.get(path, { headers });
      return res.data as ValrBalance[];
    } catch (err) {
      this.handleError(err, 'getBalances');
    }
  }

  // ─── Positions ────────────────────────────────────────────────────────────

  async getOpenPositions(currencyPair?: string): Promise<ValrOpenPosition[]> {
    const qs = currencyPair ? `?currencyPair=${currencyPair}` : '';
    const path = `/v1/positions/open${qs}`;
    try {
      const headers = this.authHeaders('GET', path);
      const res = await this.http.get(path, { headers });
      return res.data as ValrOpenPosition[];
    } catch (err) {
      this.handleError(err, 'getOpenPositions');
    }
  }

  // ─── Orders ───────────────────────────────────────────────────────────────

  async getOpenOrders(): Promise<ValrOpenOrder[]> {
    const path = '/v1/orders/open';
    try {
      const headers = this.authHeaders('GET', path);
      const res = await this.http.get(path, { headers });
      return res.data as ValrOpenOrder[];
    } catch (err) {
      this.handleError(err, 'getOpenOrders');
    }
  }

  async placeLimitOrder(order: PlaceLimitOrderRequest): Promise<OrderAcceptedResponse> {
    const path = '/v1/orders/limit';
    const body = JSON.stringify(order);
    try {
      const headers = this.authHeaders('POST', path, body);
      const res = await this.http.post(path, order, { headers });
      const data = res.data as OrderAcceptedResponse;
      log.info({ customerOrderId: order.customerOrderId, id: data.id }, 'Limit order accepted (202)');
      return data;
    } catch (err) {
      this.handleError(err, 'placeLimitOrder');
    }
  }

  async placeMarketOrder(order: PlaceMarketOrderRequest): Promise<OrderAcceptedResponse> {
    const path = '/v1/orders/market';
    const body = JSON.stringify(order);
    try {
      const headers = this.authHeaders('POST', path, body);
      const res = await this.http.post(path, order, { headers });
      const data = res.data as OrderAcceptedResponse;
      log.info({ customerOrderId: order.customerOrderId, id: data.id }, 'Market order accepted (202)');
      return data;
    } catch (err) {
      this.handleError(err, 'placeMarketOrder');
    }
  }

  async placeBatchOrders(payload: BatchOrdersPayload): Promise<BatchOrdersResponse> {
    const path = '/v1/batch/orders';
    const body = JSON.stringify(payload);
    try {
      const headers = this.authHeaders('POST', path, body);
      const res = await this.http.post(path, payload, { headers });
      return res.data as BatchOrdersResponse;
    } catch (err) {
      this.handleError(err, 'placeBatchOrders');
    }
  }

  async cancelOrder(orderId: string, pair: string): Promise<void> {
    const path = '/v1/orders/order';
    const orderBody = { orderId, pair };
    const body = JSON.stringify(orderBody);
    try {
      const headers = this.authHeaders('DELETE', path, body);
      await this.http.delete(path, { headers, data: orderBody });
      log.info({ orderId, pair }, 'Order cancelled');
    } catch (err) {
      this.handleError(err, 'cancelOrder');
    }
  }

  async cancelAllOrdersForPair(currencyPair: string): Promise<void> {
    const path = `/v1/orders/${currencyPair}`;
    try {
      const headers = this.authHeaders('DELETE', path);
      await this.http.delete(path, { headers });
      log.info({ currencyPair }, 'All orders cancelled for pair');
    } catch (err) {
      this.handleError(err, 'cancelAllOrdersForPair');
    }
  }

  // ─── Conditional Orders (TPSL) ────────────────────────────────────────────

  async placeConditionalOrder(order: PlaceConditionalOrderRequest): Promise<ConditionalOrderResponse> {
    const path = '/v1/orders/conditionals';
    const body = JSON.stringify(order);
    try {
      const headers = this.authHeaders('POST', path, body);
      const res = await this.http.post(path, order, { headers });
      const data = res.data as ConditionalOrderResponse;
      log.info({ id: data.id, pair: order.pair }, 'Conditional order accepted (202)');
      return data;
    } catch (err) {
      this.handleError(err, 'placeConditionalOrder');
    }
  }

  async getConditionalOrders(): Promise<ValrConditionalOrder[]> {
    const path = '/v1/orders/conditionals';
    try {
      const headers = this.authHeaders('GET', path);
      const res = await this.http.get(path, { headers });
      return res.data as ValrConditionalOrder[];
    } catch (err) {
      this.handleError(err, 'getConditionalOrders');
    }
  }

  async cancelConditionalOrder(orderId: string, currencyPair: string): Promise<void> {
    const path = '/v1/orders/conditionals/conditional';
    const orderBody = { orderId, currencyPair };
    const body = JSON.stringify(orderBody);
    try {
      const headers = this.authHeaders('DELETE', path, body);
      await this.http.delete(path, { headers, data: orderBody });
      log.info({ orderId, currencyPair }, 'Conditional order cancelled');
    } catch (err) {
      // Log but don't throw — conditional may already have triggered
      log.warn({ orderId, currencyPair, err }, 'Failed to cancel conditional order (may already be gone)');
    }
  }

  async cancelAllConditionalsForPair(currencyPair: string): Promise<void> {
    const path = `/v1/orders/conditionals/${currencyPair}`;
    try {
      const headers = this.authHeaders('DELETE', path);
      await this.http.delete(path, { headers });
      log.info({ currencyPair }, 'All conditionals cancelled for pair');
    } catch (err) {
      log.warn({ currencyPair, err }, 'Failed to cancel all conditionals (may already be gone)');
    }
  }
}
