/**
 * Exchange Types — VALR Perpetual Futures
 */

export interface ValrOrder {
  id?: string;         // Response from placeLimitOrder (202 Accepted)
  orderId?: string;    // Response from getOpenOrders
  customerOrderId?: string;
  pair?: string;
  currencyPair?: string;
  side: 'BUY' | 'SELL';
  type?: string;
  status?: string;
  price?: string;
  quantity?: string;
  filledQuantity?: string;
  remainingQuantity?: string;
  averagePrice?: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface ValrPosition {
  instrumentId: string;
  side: 'LONG' | 'SHORT' | 'NONE';
  openPositionQuantity: string;
  averageEntryPrice: string;
  markPrice: string;
  unrealizedPnl: string;
  leverage: string;
  marginBalance: string;
  liquidationPrice?: string;
}

export interface ValrBalance {
  asset: string;
  available: string;
  pending: string;
  inOrders: string;
}

export interface ValrTicker {
  pair: string;
  bid: string;
  ask: string;
  last: string;
  high: string;
  low: string;
  volume: string;
  timestamp: string;
}

export interface ValrMarkPrice {
  pair: string;
  markPrice: string;
  indexPrice: string;
  timestamp: string;
}

export interface ValrConditionalOrder {
  orderId: string;
  customerOrderId?: string;
  pair: string;
  triggerType: 'MARK_PRICE' | 'LAST_PRICE';
  triggerPrice: string;
  targetPrice?: string;
  side: 'BUY' | 'SELL';
  quantity: string;
  status: 'ACTIVE' | 'TRIGGERED' | 'CANCELLED' | 'EXPIRED';
  createdAt: string;
}

export interface OrderPlacement {
  pair: string;
  side: 'BUY' | 'SELL';
  quantity: string;
  price: string;
  customerOrderId?: string;
  allowMargin?: boolean;
  reduceOnly?: boolean;
  postOnly?: boolean;
}

export interface ConditionalPlacement {
  pair: string;
  triggerType: 'MARK_PRICE' | 'LAST_PRICE';
  triggerPrice: string;
  targetPrice?: string;
  side: 'BUY' | 'SELL';
  type: 'LIMIT' | 'MARKET';
  price?: string;
  quantity: string;
  customerOrderId?: string;
  subaccountId?: string;
}
