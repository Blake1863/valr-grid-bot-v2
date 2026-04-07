// Typed API shapes bound directly to VALR API docs
// Source: https://api-docs.rooibos.dev/llms-full.txt

// ─── Pair Metadata ─────────────────────────────────────────────────────────

export interface ValrPairInfo {
  symbol: string;
  baseCurrency: string;
  quoteCurrency: string;
  shortName: string;
  active: boolean;
  minBaseAmount: string;
  maxBaseAmount: string;
  minQuoteAmount: string;
  maxQuoteAmount: string;
  tickSize: string;
  baseDecimalPlaces: string;
  marginTradingAllowed: boolean;
  currencyPairType: string; // "SPOT" | "FUTURE"
  initialMarginFraction?: string;
  maintenanceMarginFraction?: string;
  autoCloseMarginFraction?: string;
}

// ─── Positions ─────────────────────────────────────────────────────────────

export interface ValrOpenPosition {
  pair: string;
  side: 'buy' | 'sell'; // lowercase from API
  quantity: string;
  realisedPnl: string;
  totalSessionEntryQuantity: string;
  totalSessionValue: string;
  sessionAverageEntryPrice: string;
  averageEntryPrice: string; // SOURCE OF TRUTH for SL/TP calculation
  unrealisedPnl: string;
  updatedAt: string;
  createdAt: string;
  positionId: string;
  leverageTier?: number;
}

// ─── Orders ────────────────────────────────────────────────────────────────

export interface PlaceLimitOrderRequest {
  side: 'BUY' | 'SELL';
  quantity: string;
  price: string;
  pair: string;
  postOnly?: boolean;
  reduceOnly?: boolean;
  customerOrderId?: string;
  timeInForce?: 'GTC' | 'FOK' | 'IOC';
  allowMargin?: boolean;
  postOnlyReprice?: boolean;
  postOnlyRepriceTicks?: string;
}

export interface PlaceMarketOrderRequest {
  side: 'BUY' | 'SELL';
  pair: string;
  baseAmount?: string;
  quoteAmount?: string;
  customerOrderId?: string;
  allowMargin?: boolean;
  timeInForce?: 'FOK' | 'IOC';
  reduceOnly?: boolean;
}

export interface OrderAcceptedResponse {
  id: string; // uuid — 202 Accepted, NOT guaranteed live
}

// ─── Conditional Orders (TPSL) ────────────────────────────────────────────

export interface PlaceConditionalOrderRequest {
  pair: string;
  quantity: string; // "0" = close entire position
  triggerType: 'MARK_PRICE' | 'LAST_TRADED';
  customerOrderId?: string;
  stopLossTriggerPrice?: string;
  stopLossOrderPrice?: string; // "-1" = market execution
  takeProfitTriggerPrice?: string;
  takeProfitOrderPrice?: string; // "-1" = market execution
  linkedOrderId?: string;
  side?: string;
  allowMargin?: boolean;
}

export interface ConditionalOrderResponse {
  id: string; // 202 — check via WS or GET /v1/orders/conditionals
}

export interface ValrConditionalOrder {
  orderId?: string;
  id?: string;
  currencyPair?: string;
  pair?: string;
  conditionalType?: string;
  triggerType?: string;
  quantity?: string;
  stopLossTriggerPrice?: string;
  stopLossPlacePrice?: string;
  takeProfitTriggerPrice?: string;
  takeProfitPlacePrice?: string;
  customerOrderId?: string;
  status?: string;
}

// ─── Batch Orders ──────────────────────────────────────────────────────────

export interface BatchOrderRequest {
  type: 'PLACE_LIMIT' | 'PLACE_MARKET' | 'CANCEL_ORDER' | 'MODIFY_ORDER';
  data: Record<string, unknown>;
}

export interface BatchOrdersPayload {
  customerBatchId?: string;
  requests: BatchOrderRequest[];
}

export interface BatchOrderOutcome {
  accepted: boolean;
  orderId?: string;
  customerOrderId?: string;
  requestType?: string;
  error?: {
    code: number;
    message: string;
  };
}

export interface BatchOrdersResponse {
  outcomes: BatchOrderOutcome[];
  batchId: number;
}

// ─── Market Summary ────────────────────────────────────────────────────────

export interface ValrMarketSummary {
  currencyPair: string;
  askPrice: string;
  bidPrice: string;
  lastTradedPrice: string;
  previousClosePrice?: string;
  baseVolume?: string;
  highPrice?: string;
  lowPrice?: string;
  created?: string;
  changeFromPrevious?: string;
  markPrice?: string; // only for futures
}

// ─── Account Balances ──────────────────────────────────────────────────────

export interface ValrBalance {
  currency: string;
  available: string;
  reserved: string;
  total: string;
  updatedAt?: string;
}

// ─── Open Orders ───────────────────────────────────────────────────────────

export interface ValrOpenOrder {
  orderId: string;
  side: string;
  remainingQuantity: string;
  price: string;
  currencyPair: string;
  createdAt: string;
  updatedAt?: string;
  type: string;
  status?: string;
  timeInForce?: string;
  customerOrderId?: string;
  postOnly?: boolean;
  stopPrice?: string;
  isReduceOnly?: boolean;
}

// ─── WebSocket Events ──────────────────────────────────────────────────────

export interface WsEnvelope<T = unknown> {
  type: string;
  data?: T;
}

export interface WsOrderStatusUpdate {
  orderId: string;
  orderStatusType: 'Placed' | 'Partially Filled' | 'Filled' | 'Cancelled' | 'Failed';
  currencyPair: string;
  originalPrice: string;
  remainingQuantity: string;
  originalQuantity: string;
  orderSide: 'BUY' | 'SELL';
  orderType: string;
  failedReason?: string | null;
  orderUpdatedAt: string;
  orderCreatedAt: string;
  customerOrderId?: string;
}

export interface WsOpenPositionUpdate {
  pair: string;
  side: 'buy' | 'sell';
  quantity: string;
  realisedPnl: string;
  totalSessionEntryQuantity: string;
  totalSessionValue: string;
  sessionAverageEntryPrice: string;
  averageEntryPrice: string; // SOURCE OF TRUTH
  unrealisedPnl: string;
  updatedAt: string;
  createdAt: string;
  positionId: string;
  leverageTier: number;
}

export interface WsPositionClosed {
  pair: string;
  positionId: string;
}

export interface WsAggregatedOrderbookUpdate {
  Asks: Array<{
    side: string;
    quantity: string;
    price: string;
    currencyPair: string;
    orderCount: number;
  }>;
  Bids: Array<{
    side: string;
    quantity: string;
    price: string;
    currencyPair: string;
    orderCount: number;
  }>;
  SequenceNumber: number;
}

export interface WsMarkPriceUpdate {
  currencyPair: string;
  markPrice: string;
}

export interface WsMarketSummaryUpdate {
  currencyPairCode?: string;
  currencyPair?: string;
  bidPrice?: string;
  askPrice?: string;
  lastTradedPrice?: string;
  markPrice?: string;
}

export interface WsFailedOrder {
  orderId?: string;
  customerOrderId?: string;
  pair?: string;
  failReason?: string;
  message?: string;
}

export interface WsOpenOrdersUpdate {
  // Array of open orders — shape similar to REST /v1/orders/open
  [key: string]: unknown;
}

// ─── Error Response ────────────────────────────────────────────────────────

export interface ValrErrorResponse {
  code: number;
  message: string;
  validationErrors?: unknown;
}
