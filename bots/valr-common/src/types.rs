use rust_decimal::Decimal;
use serde::Deserialize;

/// A grid/limit order fill event from the account WebSocket.
#[derive(Debug, Clone)]
pub struct FillEvent {
    pub order_id: String,
    pub pair: String,
    pub side: String,        // "Buy" or "Sell"
    pub status: String,      // "Filled" or "Partially Filled"
    pub filled_qty: Decimal,
    pub price: Decimal,
}

#[derive(Debug, Deserialize)]
pub struct Balance {
    pub currency: String,
    pub available: String,
    pub total: String,
}
