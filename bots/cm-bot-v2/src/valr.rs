use crate::types::{Order, OrderSide, OrderType, OrderStatus, TimeInForce, Balance};
use hmac::{Hmac, Mac};
use sha2::{Sha512, Digest};
use serde::{Deserialize, Serialize};
use std::time::{SystemTime, UNIX_EPOCH};
use std::sync::Arc;
use tokio::sync::RwLock;
use anyhow::{Result, Context};

type HmacSha512 = Hmac<Sha512>;

/// Margin info from account WebSocket
#[derive(Debug, Clone, Deserialize)]
pub struct MarginInfo {
    #[serde(default)]
    pub available_in_reference: f64,
    #[serde(default)]
    pub total_in_reference: f64,
    #[serde(default)]
    pub currency: String,
}

/// Shared margin state for fast access (updated by WebSocket, read by cycle loop)
#[derive(Debug, Clone, Default)]
pub struct MarginState {
    pub cm1_available: f64,
    pub cm2_available: f64,
    pub last_update_cm1: u64,
    pub last_update_cm2: u64,
}

pub type SharedMarginState = Arc<RwLock<MarginState>>;

#[derive(Debug, Serialize)]
struct OrderRequest {
    symbol: String,
    side: String,
    #[serde(rename = "type")]
    order_type: String,
    #[serde(rename = "timeInForce")]
    time_in_force: String,
    price: String,
    quantity: String,
    #[serde(rename = "postOnly")]
    post_only: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    customer_order_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ValrOrderResponse {
    id: String,
    #[serde(default)]
    customerOrderId: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ValrBalanceResponse {
    currency: String,
    available: String,
    reserved: String,
    #[serde(default)]
    pending: String,
    #[serde(default, rename = "totalInReference")]
    total_in_reference: String,
}

#[derive(Debug, Deserialize)]
struct ValrTickerResponse {
    #[serde(default)]
    lastPrice: Option<String>,
    #[serde(default)]
    price: Option<String>,
    #[serde(default)]
    markPrice: Option<String>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct ValrPairResponse {
    pub symbol: String,
    #[serde(rename = "tickSize")]
    pub tick_size: String,
    #[serde(rename = "baseDecimalPlaces")]
    pub base_decimal_places: String,
    #[serde(rename = "minBaseAmount")]
    pub min_base_amount: String,
    #[serde(rename = "minQuoteAmount")]
    pub min_quote_amount: String,
}

pub struct ValrClient {
    api_key: String,
    api_secret: String,
    subaccount_id: Option<String>,
    base_url: String,
    client: reqwest::Client,
}

impl ValrClient {
    pub fn new(api_key: String, api_secret: String, subaccount_id: Option<String>, base_url: String) -> Self {
        Self {
            api_key,
            api_secret,
            subaccount_id,
            base_url,
            client: reqwest::Client::new(),
        }
    }

    fn timestamp_ms(&self) -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as u64
    }

    fn add_auth_headers(&self, builder: reqwest::RequestBuilder, method: &str, path: &str, body: &str) -> reqwest::RequestBuilder {
        let timestamp = self.timestamp_ms();
        let sig = self.generate_signature(method, path, timestamp, body);
        let mut builder = builder
            .header("X-VALR-API-KEY", &self.api_key)
            .header("X-VALR-TIMESTAMP", timestamp.to_string())
            .header("X-VALR-SIGNATURE", sig);
        
        if let Some(subaccount_id) = &self.subaccount_id {
            builder = builder.header("X-VALR-SUB-ACCOUNT-ID", subaccount_id);
        }
        
        builder
    }

    fn generate_signature(&self, method: &str, path: &str, timestamp: u64, body: &str) -> String {
        // VALR requires: timestamp + verb + path + body + subaccountId
        let subaccount_id = self.subaccount_id.as_deref().unwrap_or("");
        let message = format!("{}{}{}{}{}", timestamp, method.to_uppercase(), path, body, subaccount_id);
        let mut mac = HmacSha512::new_from_slice(self.api_secret.as_bytes()).unwrap();
        mac.update(message.as_bytes());
        let result = mac.finalize();
        hex::encode(result.into_bytes())
    }

    pub async fn place_order(
        &self,
        symbol: &str,
        side: OrderSide,
        order_type: OrderType,
        time_in_force: TimeInForce,
        price: f64,
        quantity: f64,
        post_only: bool,
    ) -> Result<Order> {
        let timestamp = self.timestamp_ms();
        
        let tif_str = match time_in_force {
            TimeInForce::GTC => "GTC",
            TimeInForce::IOC => "IOC",
            TimeInForce::FOK => "FOK",
        };

        // For GTC post-only maker orders: use the reprice endpoint (/v1/orders/limit)
        // with postOnlyReprice=true so the order always lands on the book at best bid/ask.
        // For IOC taker orders: use the standard endpoint (/v2/orders/limit).
        let (path, request) = if post_only && time_in_force == TimeInForce::GTC {
            let p = "/v1/orders/limit";
            let r = serde_json::json!({
                "side": side.to_string().to_uppercase(),
                "quantity": format!("{}", quantity),
                "price": format!("{}", price),
                "pair": symbol,
                "postOnlyReprice": true,
                "postOnlyRepriceTicks": "1",
                "timeInForce": "GTC",
                "customerOrderId": uuid::Uuid::new_v4().to_string()
            });
            (p, r)
        } else {
            let p = "/v2/orders/limit";
            let r = serde_json::json!({
                "side": side.to_string().to_uppercase(),
                "quantity": format!("{}", quantity),
                "price": format!("{}", price),
                "pair": symbol,
                "postOnly": false,
                "timeInForce": tif_str,
                "customerOrderId": uuid::Uuid::new_v4().to_string()
            });
            (p, r)
        };

        let body = serde_json::to_string(&request)
            .context("Failed to serialize order request")?;

        let response = self.add_auth_headers(
            self.client
                .post(format!("{}{}", self.base_url, path))
                .header("Content-Type", "application/json")
                .body(body.clone()),
            "POST",
            path,
            &body
        )
        .send()
        .await
        .context("Failed to send order request")?;

        if response.status().is_success() {
            let valr_order: ValrOrderResponse = response.json().await
                .context("Failed to parse order response")?;
            Ok(Order {
                id: valr_order.id,
                symbol: symbol.to_string(),
                side,
                order_type,
                time_in_force,
                price,
                quantity,
                filled_quantity: 0.0,
                status: OrderStatus::Pending,
                created_at: chrono::Utc::now(),
            })
        } else {
            let status = response.status();
            let text = response.text().await
                .context("Failed to read error response")?;
            anyhow::bail!("Order failed ({}): {}", status, text)
        }
    }

    pub async fn get_open_orders(&self, symbol: Option<&str>) -> Result<Vec<Order>> {
        let path = "/v1/orders/open";
        let (url, auth_path) = match symbol {
            Some(s) => {
                let query = format!("?pair={}", s);
                (format!("{}{}{}", self.base_url, path, query), format!("{}{}", path, query))
            },
            None => (format!("{}{}", self.base_url, path), path.to_string()),
        };

        let response = self.add_auth_headers(
            self.client.get(&url),
            "GET",
            &auth_path,
            ""
        )
        .send()
        .await
        .context("Failed to send get open orders request")?;

        if response.status().is_success() {
            let raw: Vec<serde_json::Value> = response.json().await
                .context("Failed to parse open orders response")?;
            
            let orders = raw.iter().filter_map(|o| {
                let id = o["orderId"].as_str()?.to_string();
                let pair = o["currencyPair"].as_str()?.to_string();
                let side_str = o["side"].as_str().unwrap_or("buy");
                let side = if side_str.to_lowercase() == "sell" { 
                    crate::types::OrderSide::Sell 
                } else { 
                    crate::types::OrderSide::Buy 
                };
                let price = o["price"].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                let quantity = o["originalQuantity"].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                let filled = o["filledPercentage"].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0) / 100.0 * quantity;
                let created_at = o["createdAt"].as_str()
                    .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
                    .map(|dt| dt.with_timezone(&chrono::Utc))
                    .unwrap_or_else(chrono::Utc::now);
                
                Some(crate::types::Order {
                    id,
                    symbol: pair,
                    side,
                    order_type: crate::types::OrderType::Limit,
                    time_in_force: crate::types::TimeInForce::GTC,
                    price,
                    quantity,
                    filled_quantity: filled,
                    status: crate::types::OrderStatus::Open,
                    created_at,
                })
            }).collect();
            
            Ok(orders)
        } else {
            anyhow::bail!("Failed to get open orders: {}", response.status())
        }
    }

    pub async fn cancel_order(&self, symbol: &str, order_id: &str) -> Result<()> {
        let path = "/v1/orders/order";

        let cancel_request = serde_json::json!({
            "orderId": order_id,
            "pair": symbol
        });

        let body = serde_json::to_string(&cancel_request)
            .context("Failed to serialize cancel request")?;

        let response = self.add_auth_headers(
            self.client
                .delete(format!("{}{}", self.base_url, path))
                .header("Content-Type", "application/json")
                .body(body.clone()),
            "DELETE",
            path,
            &body
        )
        .send()
        .await
        .context("Failed to send cancel order request")?;

        if response.status().is_success() {
            Ok(())
        } else {
            let status = response.status();
            let text = response.text().await
                .context("Failed to read error response")?;
            anyhow::bail!("Failed to cancel order ({}): {}", status, text)
        }
    }

    pub async fn get_balances(&self) -> Result<Vec<Balance>> {
        let path = "/v1/account/balances";

        let response = self.add_auth_headers(
            self.client.get(format!("{}{}", self.base_url, path)),
            "GET",
            path,
            ""
        )
        .send()
        .await
        .context("Failed to send get balances request")?;

        if response.status().is_success() {
            let balances: Vec<ValrBalanceResponse> = response.json().await
                .context("Failed to parse balances response")?;
            Ok(balances
                .into_iter()
                .map(|b| Balance {
                    asset: b.currency.clone(),
                    available: b.available.parse().unwrap_or(0.0),
                    pending: b.reserved.parse().unwrap_or(0.0),
                    total_in_reference: b.total_in_reference.parse().unwrap_or(0.0),
                })
                .collect())
        } else {
            anyhow::bail!("Failed to get balances: {}", response.status())
        }
    }

    /// Get available margin in reference currency (USDC equivalent).
    /// Uses `totalInReference` on the USDT balance — this is what the WS BALANCE_UPDATE
    /// event sends as `availableInReference` and correctly reflects usable futures margin
    /// including unrealised PnL. The `available` field on the REST response is the spot
    /// available amount, which excludes margin locked in futures positions.
    pub async fn get_available_in_reference(&self) -> Result<f64> {
        let balances = self.get_balances().await?;
        if let Some(usdt) = balances.iter().find(|b| b.asset == "USDT") {
            if usdt.total_in_reference > 0.0 {
                return Ok(usdt.total_in_reference);
            }
        }
        // Fallback: sum totalInReference across all currencies
        let total_ref: f64 = balances.iter().map(|b| b.total_in_reference).sum();
        Ok(total_ref)
    }

    pub async fn get_ticker(&self, symbol: &str) -> Result<f64> {
        let path = format!("/pairs/{}/ticker", symbol);
        
        let response = self.client
            .get(format!("{}{}", self.base_url, path))
            .send()
            .await
            .context("Failed to send ticker request")?;

        if response.status().is_success() {
            let ticker: ValrTickerResponse = response.json().await
                .context("Failed to parse ticker response")?;
            
            let price_str = ticker.lastPrice
                .or_else(|| ticker.price)
                .or_else(|| ticker.markPrice)
                .ok_or_else(|| anyhow::anyhow!("No price field in ticker response"))?;
            
            price_str.parse::<f64>()
                .context("Failed to parse price as f64")
        } else {
            anyhow::bail!("Failed to get ticker: {}", response.status())
        }
    }

    pub async fn get_all_pairs(&self) -> Result<Vec<ValrPairResponse>> {
        // Public endpoint - no auth required
        let url = "https://api.valr.com/v1/public/pairs";
        
        let response = self.client
            .get(url)
            .send()
            .await
            .context("Failed to send get pairs request")?;

        if response.status().is_success() {
            let pairs: Vec<ValrPairResponse> = response.json().await
                .context("Failed to parse pairs response")?;
            Ok(pairs)
        } else {
            anyhow::bail!("Failed to get pairs: {}", response.status())
        }
    }

    /// Generate signature for WebSocket authentication
    pub fn generate_ws_signature(&self) -> (String, String) {
        let timestamp = self.timestamp_ms();
        let signature = self.generate_signature("GET", "/ws/account", timestamp, "");
        (timestamp.to_string(), signature)
    }
}

/// Connect to VALR account WebSocket and subscribe to margin updates
/// 
/// NOTE: Currently disabled due to tokio-tungstenite header limitations.
/// Production uses REST API fallback which works perfectly.
/// See WEBSOCKET_NOTES.md for details.
pub async fn connect_account_ws(
    _api_key: String,
    _api_secret: String,
    _subaccount_id: Option<String>,
    _margin_state: SharedMarginState,
    account_name: &str,
) -> Result<()> {
    // WebSocket auth requires custom headers which tokio-tungstenite 0.20 doesn't support properly
    // REST API fallback is working perfectly (~200-500ms per call, well within 15s cycle)
    
    tracing::info!("[{}] Account WebSocket disabled - using REST API for balance checks", account_name);
    
    // Keep task alive but do nothing
    loop {
        tokio::time::sleep(tokio::time::Duration::from_secs(300)).await;
    }
}
