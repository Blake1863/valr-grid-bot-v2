use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum PairType {
    Spot,
    Perp,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderSide {
    Buy,
    Sell,
}

impl OrderSide {
    pub fn opposite(&self) -> Self {
        match self {
            OrderSide::Buy => OrderSide::Sell,
            OrderSide::Sell => OrderSide::Buy,
        }
    }
}

impl std::fmt::Display for OrderSide {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OrderSide::Buy => write!(f, "BUY"),
            OrderSide::Sell => write!(f, "SELL"),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderType {
    Limit,
    Market,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimeInForce {
    GTC,
    IOC,
    FOK,
}

#[derive(Debug, Clone)]
pub struct Order {
    pub id: String,
    pub symbol: String,
    pub side: OrderSide,
    pub order_type: OrderType,
    pub time_in_force: TimeInForce,
    pub price: f64,
    pub quantity: f64,
    pub filled_quantity: f64,
    pub status: OrderStatus,
    pub created_at: chrono::DateTime<chrono::Utc>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderStatus {
    Pending,
    Open,
    Filled,
    PartiallyFilled,
    Cancelled,
    Rejected,
}

#[derive(Debug, Clone)]
pub struct Balance {
    pub asset: String,
    pub available: f64,
    pub pending: f64,
    pub total_in_reference: f64,
}

#[derive(Debug, Clone)]
pub struct PairState {
    pub cycle_count: u64,
    pub current_phase: u64,
    pub maker_account: String,
    pub maker_side: OrderSide,
    pub last_cycle_time: chrono::DateTime<chrono::Utc>,
    pub total_trades: u64,
    pub external_fills: u64,
}

impl Default for PairState {
    fn default() -> Self {
        Self {
            cycle_count: 0,
            current_phase: 0,
            maker_account: "CM1".to_string(),
            maker_side: OrderSide::Buy,
            last_cycle_time: chrono::Utc::now(),
            total_trades: 0,
            external_fills: 0,
        }
    }
}
