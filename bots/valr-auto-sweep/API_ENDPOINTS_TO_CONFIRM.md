# VALR API Endpoints - All Confirmed ✅

## ✅ All Endpoints Confirmed & Implemented

### Balances
```
GET /v1/account/balances
Auth: Yes (HMAC-SHA512)
Response: [{"currency": "ZAR", "available": "0", "pending": "0"}, ...]
```

### Orderbook (Public)
```
GET /v1/public/{pair}/orderbook
Auth: No
Response: {"Bids": [...], "Asks": [...]}
Note: PascalCase (Bids/Asks, not bids/asks)
```

### Market Order
```
POST /v1/orders/{pair}/market
Auth: Yes (HMAC-SHA512)
Body: {"side": "BUY", "quantity": "1.0", "orderType": "MARKET"}
```

### VALR Pay Transfer ✅
```
POST /v1/pay
Auth: Yes (HMAC-SHA512)
Body: {
  "currency": "SOL",
  "amount": 1.0,  // Float, not string
  "recipientPayId": "M3Q5AEUP8W99QZR6T3ZS",
  "anonymous": "false"
}
Response: 202 Accepted with {id, transactionId}
Notes:
- Only ONE of: recipientEmail, recipientCellNumber, recipientPayId
- Amount is float
```

### SOL Staking ✅
```
POST /v1/staking/stake
Auth: Yes (HMAC-SHA512)
Body: {
  "currencySymbol": "SOL",
  "amount": "1.0",  // String, not float
  "earnType": "STAKE"
}
Notes:
- Amount is string
- earnType must be "STAKE"
- Subsequent calls increase locked amount
```

---

## Ready for Testing

All endpoints are now implemented in `main.py`. Next steps:

1. Add AutobuySol credentials to secrets
2. Run `test_endpoints.py` to verify auth
3. Fund AutobuySol with small ZAR amount
4. Let bot run and verify full flow
