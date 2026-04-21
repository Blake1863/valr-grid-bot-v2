# VALR API Reference — Memorized

## Authentication

**Signature Formula:**
```
Signature = HMAC-SHA512(API_SECRET, timestamp + verb + path + body + subaccountId)
```

**Headers:**
- `X-VALR-API-KEY`: Your API key
- `X-VALR-SIGNATURE`: HMAC-SHA512 signature
- `X-VALR-TIMESTAMP`: Unix timestamp in milliseconds
- `X-VALR-SUB-ACCOUNT-ID`: Subaccount ID (optional)

---

## WebSocket Channels

### Account Channel (`wss://api.valr.com/ws/account`)

**Auth:** Required (headers or in-band AUTHENTICATE)

**Auto-subscribed Events:**
- `BALANCE_UPDATE` — Balance changes
- `OPEN_ORDERS_UPDATE` — Open order changes
- `ORDER_PROCESSED` — Order processing results
- `FAILED_ORDER` — Failed order placement
- `FAILED_CANCEL_ORDER` — Failed cancellation
- `NEW_ACCOUNT_TRADE` — Trades (orderbook only)
- `NEW_ACCOUNT_HISTORY_RECORD` — Transaction history
- `INSTANT_ORDER_COMPLETED` — Simple buy/sell completed
- `NEW_PENDING_RECEIVE` — Pending crypto deposits
- `NEW_PENDING_SEND` — Pending crypto withdrawals
- `SEND_STATUS_UPDATE` — Withdrawal status

**Subscription-required Events:**
- `ORDER_STATUS_UPDATE` — Order status changes
- `MARGIN_INFO` — Margin info (beta, 5s interval)

**Commands (Client → Server):**
- `PLACE_LIMIT_ORDER`
- `PLACE_MARKET_ORDER`
- `CANCEL_ORDER`
- `MODIFY_ORDER`
- `PLACE_BATCH_ORDERS`

**Command Format:**
```json
{
    "type": "COMMAND_TYPE",
    "clientMsgId": "unique-correlation-id",
    "payload": { ... }
}
```

### Trade Channel (`wss://api.valr.com/ws/trade`)

**Events (subscription required):**
- `AGGREGATED_ORDERBOOK_UPDATE` — Full orderbook by price level
- `ALLOWED_ORDER_TYPES_UPDATED` — Allowed order types changed

---

## Key REST Endpoints

### Account

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/v1/account/balances` | Get account balances |
| GET | `/v1/positions/open` | Get open positions |
| GET | `/v1/orders/open` | Get open orders |
| GET | `/v1/orders/history` | Get order history |
| GET | `/v1/account/trades` | Get trade history |

### Orders

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/v1/orders/limit` | Place limit order |
| POST | `/v2/orders/limit` | Place limit order (v2) |
| POST | `/v1/orders/market` | Place market order |
| DELETE | `/v1/orders/order` | Cancel order (202 Accepted) |
| DELETE | `/v2/orders/order` | Cancel order (200 OK) |
| DELETE | `/v1/orders` | Cancel all orders |
| DELETE | `/v1/orders/{currencyPair}` | Cancel all for pair |

### Subaccounts

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/v1/account/subaccounts/transfer` | Transfer between accounts |

**Request Body:**
```json
{
    "fromId": 0,
    "toId": "SUBACCOUNT_ID",
    "currencyCode": "USDT",
    "amount": "100.00",
    "allowBorrow": false
}
```

### Lending / Staking

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/v1/staking/un-stake` | Unlock from DeFi lending |
| DELETE | `/v1/loans/unlock` | Cancel unlock request |

**Un-stake Request Body:**
```json
{
    "currencySymbol": "USDT",
    "amount": "200",
    "earnType": "LEND"
}
```

### Public (No Auth)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/v1/public/pairs` | Get all currency pairs |
| GET | `/v1/public/{pair}/orderbook` | Get orderbook |
| GET | `/v1/public/{pair}/trades` | Get trade history |
| GET | `/v1/public/currencies` | Get all currencies |

---

## WebSocket Order Placement

### PLACE_LIMIT_ORDER

```json
{
    "type": "PLACE_LIMIT_ORDER",
    "clientMsgId": "uuid-here",
    "payload": {
        "side": "BUY",
        "quantity": "0.05",
        "price": "85.91",
        "pair": "SOLUSDTPERP",
        "postOnly": true,
        "timeInForce": "GTC",
        "customerOrderId": "uuid-here"
    }
}
```

**Response:** `PLACE_LIMIT_WS_RESPONSE`
```json
{
    "type": "PLACE_LIMIT_WS_RESPONSE",
    "clientMsgId": "uuid-here",
    "data": {
        "orderId": "019daf8e-..."
    }
}
```

### CANCEL_LIMIT_ORDER

```json
{
    "type": "CANCEL_LIMIT_ORDER",
    "clientMsgId": "uuid-here",
    "payload": {
        "orderId": "019daf8e-...",
        "pair": "SOLUSDTPERP"
    }
}
```

---

## Balance Update Schema

```json
{
    "type": "BALANCE_UPDATE",
    "data": {
        "currency": {"shortName": "USDT", ...},
        "available": "100.30",
        "reserved": "0.00",
        "total": "100.30",
        "lendReserved": "0.00",
        "borrowCollateralReserved": "0.00",
        "borrowedAmount": "0.00",
        "totalInReference": "100.30",
        "referenceCurrency": "USDT"
    }
}
```

---

## Trade Event Schema

```json
{
    "type": "NEW_ACCOUNT_TRADE",
    "data": {
        "currencyPair": "SOLUSDTPERP",
        "customerOrderId": "uuid",
        "fee": "0.0008591",
        "feeCurrency": "USDT",
        "id": "019daf8e-...",
        "orderId": "019daf8e-...",
        "price": "85.91",
        "quantity": "0.05",
        "side": "buy",
        "tradedAt": "2026-04-21T10:20:13.479Z"
    }
}
```

---

## Rate Limits

- **2,000 req/min** per API key
- **1,200 req/min** per IP

---

## Error Codes

| Code | Description |
|------|-------------|
| -11252 | Invalid signature |
| -19236 | No existing unlock request |

---

## Cache Durations

| Endpoint | max-age |
|----------|---------|
| `/marketsummary` | 60s |
| `/:currencyPair/orderbook` | 30s |
| `/v1/account/balances` | 1s |

---

## Reference Files in Workspace

- `/home/admin/.openclaw/workspace/skills/valr-exchange/` — Full skill docs
- `/home/admin/.openclaw/workspace/DEPLOYMENT.md` — Current bot status
- `/home/admin/.openclaw/workspace/MEMORY.md` — Long-term memory
