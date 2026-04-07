/// Automatic xstock liquidator — detects insufficient balance and sells excess inventory.
///
/// Triggered when:
/// 1. Maker order fails with "Insufficient Balance" N times for a pair
/// 2. Account has excess base asset (>60% of combined CMS1+CMS2)
/// 3. Account is low on quote currency (< min_quote_threshold)
///
/// Uses REST API to place IOC sell orders at aggressive prices.
use anyhow::{Context, Result};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::sync::RwLock;

/// Trigger liquidation after this many consecutive maker failures per pair
const FAILURE_THRESHOLD: u32 = 3;

/// Don't liquidate if quote balance is above this (USD equivalent)
const MIN_QUOTE_THRESHOLD: f64 = 50.0;

/// Keep this reserve of base asset after liquidation
const BASE_RESERVE_PCT: f64 = 0.10;

/// Track failure counts per (account, pair)
pub struct Liquidator {
    api_key: String,
    api_secret: String,
    cms1_id: u64,
    cms2_id: u64,
    failures: Arc<RwLock<HashMap<(String, String), u32>>>, // (account, pair) -> count
    client: reqwest::Client,
}

impl Liquidator {
    pub fn new(api_key: String, api_secret: String, cms1_id: u64, cms2_id: u64) -> Self {
        Self {
            api_key,
            api_secret,
            cms1_id,
            cms2_id,
            failures: Arc::new(RwLock::new(HashMap::new())),
            client: reqwest::Client::new(),
        }
    }

    /// Record a maker order failure. Returns true if liquidation should be triggered.
    pub async fn record_failure(&self, account: &str, pair: &str) -> bool {
        let mut failures = self.failures.write().await;
        let count = failures.entry((account.to_string(), pair.to_string())).or_insert(0);
        *count = count.saturating_add(1);
        *count >= FAILURE_THRESHOLD
    }

    /// Reset failure count after successful order
    pub async fn reset_failure(&self, account: &str, pair: &str) {
        let mut failures = self.failures.write().await;
        failures.remove(&(account.to_string(), pair.to_string()));
    }

    fn timestamp_ms(&self) -> u64 {
        SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64
    }

    fn sign(&self, method: &str, path: &str, body: &str, subaccount: u64) -> (u64, String) {
        use hmac::{Hmac, Mac};
        use sha2::Sha512;
        let ts = self.timestamp_ms();
        let sub_str = if subaccount == 0 { String::new() } else { subaccount.to_string() };
        let msg = format!("{}{}{}{}{}", ts, method.to_uppercase(), path, body, sub_str);
        let mut mac = Hmac::<Sha512>::new_from_slice(self.api_secret.as_bytes()).unwrap();
        mac.update(msg.as_bytes());
        (ts, hex::encode(mac.finalize().into_bytes()))
    }

    /// Get balance for a specific currency on a subaccount
    async fn get_balance(&self, subaccount: u64, currency: &str) -> Result<f64> {
        let path = "/v1/account/balances";
        let (ts, sig) = self.sign("GET", path, "", subaccount);

        let resp = self.client
            .get(format!("https://api.valr.com{}", path))
            .header("X-VALR-API-KEY", &self.api_key)
            .header("X-VALR-SIGNATURE", sig)
            .header("X-VALR-TIMESTAMP", ts.to_string())
            .header("X-VALR-SUB-ACCOUNT-ID", subaccount.to_string())
            .send().await
            .context("Failed to fetch balances")?;

        #[derive(serde::Deserialize)]
        struct Balance {
            currency: String,
            available: String,
        }

        let balances: Vec<Balance> = resp.json().await.context("Failed to parse balances")?;
        for b in balances {
            if b.currency == currency {
                return b.available.parse().context("Failed to parse balance");
            }
        }
        Ok(0.0)
    }

    /// Get current price from orderbook
    async fn get_price(&self, pair: &str) -> Result<f64> {
        let resp = self.client
            .get(format!("https://api.valr.com/v1/public/{}/orderbook", pair))
            .send().await
            .context("Failed to fetch orderbook")?;

        #[derive(serde::Deserialize)]
        struct Order {
            price: String,
        }

        #[derive(serde::Deserialize)]
        struct Orderbook {
            #[serde(default)]
            Bids: Vec<Order>,
            #[serde(default)]
            bids: Vec<Order>,
        }

        let data: Orderbook = resp.json().await.context("Failed to parse orderbook")?;
        let bids = if !data.Bids.is_empty() { &data.Bids } else { &data.bids };
        
        bids.first()
            .map(|b| b.price.parse::<f64>().unwrap_or(0.0))
            .context("No bids in orderbook")
    }

    /// Place IOC limit sell order via REST API
    async fn place_sell(&self, subaccount: u64, pair: &str, quantity: f64, price: f64) -> Result<String> {
        let path = "/v1/orders/limit";
        let body = serde_json::json!({
            "pair": pair,
            "side": "SELL",
            "quantity": format!("{:.8}", quantity),
            "price": format!("{}", price),
            "timeInForce": "IOC",
            "customerOrderId": format!("liq-{}-{}", pair, self.timestamp_ms())
        });
        let body_str = serde_json::to_string(&body)?;

        let (ts, sig) = self.sign("POST", path, &body_str, subaccount);

        let resp = self.client
            .post(format!("https://api.valr.com{}", path))
            .header("X-VALR-API-KEY", &self.api_key)
            .header("X-VALR-SIGNATURE", sig)
            .header("X-VALR-TIMESTAMP", ts.to_string())
            .header("X-VALR-SUB-ACCOUNT-ID", subaccount.to_string())
            .header("Content-Type", "application/json")
            .body(body_str)
            .send().await
            .context("Failed to place order")?;

        #[derive(serde::Deserialize)]
        struct OrderResponse {
            id: String,
        }

        let result: OrderResponse = resp.json().await.context("Failed to parse order response")?;
        Ok(result.id)
    }

    /// Check if liquidation is needed and execute it
    pub async fn maybe_liquidate(&self, account: &str, pair: &str, base_currency: &str, quote_currency: &str, price_precision: u8) -> Result<bool> {
        let subaccount = if account == "CMS1" { self.cms1_id } else { self.cms2_id };
        let other_subaccount = if account == "CMS1" { self.cms2_id } else { self.cms1_id };

        // Check quote balance
        let quote_balance = self.get_balance(subaccount, quote_currency).await?;
        if quote_balance >= MIN_QUOTE_THRESHOLD {
            return Ok(false); // Have enough quote currency
        }

        // Check base balance on this account
        let base_balance = self.get_balance(subaccount, base_currency).await?;
        if base_balance < 0.01 {
            return Ok(false); // No base asset to sell
        }

        // Check other account's base balance
        let other_base = self.get_balance(other_subaccount, base_currency).await?;
        let combined = base_balance + other_base;
        
        // Check if this account has >60% of combined base assets
        if base_balance < combined * 0.60 {
            return Ok(false); // Not imbalanced
        }

        // Get current price
        let price = match self.get_price(pair).await {
            Ok(p) => p,
            Err(_) => return Ok(false),
        };

        // Calculate sell quantity (keep 10% reserve)
        let sell_qty = base_balance * (1.0 - BASE_RESERVE_PCT);
        if sell_qty < 0.001 {
            return Ok(false); // Too small to sell
        }

        // Aggressive pricing: 2% below best bid
        let factor = 10f64.powi(price_precision as i32);
        let sell_price = (price * 0.98 * factor).round() / factor;

        println!("[LIQUIDATE] {} {}: Selling {:.4} @ {} (balance: {:.4}, other: {:.4}, quote: {:.2})",
            account, pair, sell_qty, sell_price, base_balance, other_base, quote_balance);

        match self.place_sell(subaccount, pair, sell_qty, sell_price).await {
            Ok(order_id) => {
                println!("[LIQUIDATE] ✅ {} {}: Order {} placed", account, pair, order_id);
                // Reset failure count after successful liquidation
                self.reset_failure(account, pair).await;
                Ok(true)
            }
            Err(e) => {
                eprintln!("[LIQUIDATE] ❌ {} {}: {}", account, pair, e);
                Ok(false)
            }
        }
    }
}
