/// Chart Maintenance Bot - Rust Implementation
/// 
/// Executes wash trades for chart maintenance on VALR futures pairs.
/// Uses WebSocket for price feeds, REST for order placement.
/// Fires maker + taker orders with 1ms delay for maximum internal fill rate.

use anyhow::{Context, Result};
use chrono::Utc;
use futures_util::{SinkExt, StreamExt};
use hmac::{Hmac, Mac};
use rand::Rng;
use reqwest::Client;
use rust_decimal::prelude::*;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::Deserialize;
use sha2::Sha512;
use std::collections::HashMap;
use std::fs;
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::RwLock;
use tokio::time;
use tracing::{info, warn, error};

// ──────────────────────────────────────────────
// Configuration
// ──────────────────────────────────────────────

#[derive(Debug, Clone, Deserialize)]
struct Config {
    test_pair: String,
    test_spot_pair: Option<String>,
    phase1_pairs: Vec<String>,
    phase2_pairs_enabled: Option<Vec<String>>,
    cycle_interval_seconds: u64,
    inventory_cycles_buffer: u32,
    rebalance_threshold_cycles: u32,
    rebalance_interval_seconds: u64,
    qty_range_min_multiplier: f64,
    qty_range_max_multiplier: f64,
    api_base_url: Option<String>,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            test_pair: "SOLUSDTPERP".to_string(),
            test_spot_pair: None,
            phase1_pairs: vec![
                "BTCUSDTPERP".to_string(),
                "ETHUSDTPERP".to_string(),
                "SOLUSDTPERP".to_string(),
                "XRPUSDTPERP".to_string(),
                "DOGEUSDTPERP".to_string(),
                "AVAXUSDTPERP".to_string(),
            ],
            phase2_pairs_enabled: None,
            cycle_interval_seconds: 15,
            inventory_cycles_buffer: 4,
            rebalance_threshold_cycles: 2,
            rebalance_interval_seconds: 300,
            qty_range_min_multiplier: 1.1,
            qty_range_max_multiplier: 3.0,
            api_base_url: Some("https://api.valr.com/v1".to_string()),
        }
    }
}

// ──────────────────────────────────────────────
// Pair Info
// ──────────────────────────────────────────────

#[derive(Debug, Clone)]
struct PairInfo {
    symbol: String,
    pair_type: String, // "spot" or "futures"
    price_precision: u32,
    qty_precision: u32,
    min_qty: Decimal,
    min_value: Decimal, // Minimum order value in quote currency
}

impl PairInfo {
    fn new(symbol: &str, pair_type: &str, price_precision: u32, qty_precision: u32, min_qty: Decimal, min_value: Decimal) -> Self {
        Self {
            symbol: symbol.to_string(),
            pair_type: pair_type.to_string(),
            price_precision,
            qty_precision,
            min_qty,
            min_value,
        }
    }
}

// ──────────────────────────────────────────────
// Credentials
// ──────────────────────────────────────────────

#[derive(Clone)]
struct AccountCreds {
    key: String,
    secret: String,
}

#[derive(Clone)]
struct Credentials {
    cm1: AccountCreds,
    cm2: AccountCreds,
}

impl Credentials {
    fn load() -> Result<Self> {
        let secrets_path = "/home/admin/.openclaw/secrets/cm_secrets.env";
        let content = fs::read_to_string(secrets_path)
            .context(format!("Failed to read secrets from {}", secrets_path))?;
        
        let mut cm1_key = String::new();
        let mut cm1_secret = String::new();
        let mut cm2_key = String::new();
        let mut cm2_secret = String::new();
        
        for line in content.lines() {
            let line = line.trim();
            if line.starts_with('#') || line.is_empty() {
                continue;
            }
            if let Some((key, value)) = line.split_once('=') {
                match key.trim() {
                    "CM1_API_KEY" => cm1_key = value.trim().to_string(),
                    "CM1_API_SECRET" => cm1_secret = value.trim().to_string(),
                    "CM2_API_KEY" => cm2_key = value.trim().to_string(),
                    "CM2_API_SECRET" => cm2_secret = value.trim().to_string(),
                    _ => {}
                }
            }
        }
        
        if cm1_key.is_empty() || cm1_secret.is_empty() || cm2_key.is_empty() || cm2_secret.is_empty() {
            anyhow::bail!("Missing API keys in secrets file");
        }
        
        Ok(Self {
            cm1: AccountCreds { key: cm1_key, secret: cm1_secret },
            cm2: AccountCreds { key: cm2_key, secret: cm2_secret },
        })
    }
}

impl AccountCreds {
    fn sign(&self, timestamp_ms: i64, method: &str, path: &str, body: &str) -> String {
        let message = format!("{}{}{}{}", timestamp_ms, method.to_uppercase(), path, body);
        let mut mac = Hmac::<Sha512>::new_from_slice(self.secret.as_bytes())
            .expect("HMAC accepts any key size");
        mac.update(message.as_bytes());
        hex::encode(mac.finalize().into_bytes())
    }
}

// ──────────────────────────────────────────────
// Price Feed (WebSocket)
// ──────────────────────────────────────────────

struct PriceFeed {
    prices: Arc<RwLock<HashMap<String, Decimal>>>,
    bids: Arc<RwLock<HashMap<String, Decimal>>>,
    asks: Arc<RwLock<HashMap<String, Decimal>>>,
    last_update: Arc<RwLock<HashMap<String, i64>>>,
}

impl PriceFeed {
    fn new() -> Self {
        Self {
            prices: Arc::new(RwLock::new(HashMap::new())),
            bids: Arc::new(RwLock::new(HashMap::new())),
            asks: Arc::new(RwLock::new(HashMap::new())),
            last_update: Arc::new(RwLock::new(HashMap::new())),
        }
    }
    
    async fn get_bid(&self, symbol: &str) -> Option<Decimal> {
        self.bids.read().await.get(symbol).copied()
    }
    
    async fn get_ask(&self, symbol: &str) -> Option<Decimal> {
        self.asks.read().await.get(symbol).copied()
    }
    
    async fn spawn_listener(symbols: Vec<String>) -> Self {
        let feed = Self::new();
        let prices = Arc::clone(&feed.prices);
        let bids = Arc::clone(&feed.bids);
        let asks = Arc::clone(&feed.asks);
        let last_update = Arc::clone(&feed.last_update);
        
        tokio::spawn(async move {
            let ws_url = "wss://api.valr.com/ws/trade";
            
            loop {
                info!("Connecting price feed WS: {}", ws_url);
                
                match tokio_tungstenite::connect_async(ws_url).await {
                    Ok((ws_stream, _)) => {
                        info!("Price feed WS connected ✅");
                        let (mut write, mut read) = ws_stream.split();
                        
                        // Subscribe to all symbols
                        for symbol in &symbols {
                            let sub_msg = serde_json::json!({
                                "type": "SUBSCRIBE",
                                "currencyPair": symbol,
                                "messageTypes": ["OB_L1_DIFF"]
                            });
                            if let Err(e) = write.send(tokio_tungstenite::tungstenite::Message::Text(sub_msg.to_string())).await {
                                error!("Failed to subscribe to {}: {}", symbol, e);
                            }
                        }
                        
                        // Read messages
                        while let Some(Ok(msg)) = StreamExt::next(&mut read).await {
                            match msg {
                                tokio_tungstenite::tungstenite::Message::Text(text) => {
                                    if let Ok(data) = serde_json::from_str::<serde_json::Value>(&text) {
                                        if data["type"] == "OB_L1_DIFF" {
                                            if let Some(symbol) = data["currencyPair"].as_str() {
                                                if let (Some(bid_arr), Some(ask_arr)) = (data["bids"].as_array(), data["asks"].as_array()) {
                                                    if !bid_arr.is_empty() && !ask_arr.is_empty() {
                                                        if let (Some(bid), Some(ask)) = (bid_arr[0]["price"].as_str(), ask_arr[0]["price"].as_str()) {
                                                            if let (Ok(bid_d), Ok(ask_d)) = (Decimal::from_str(bid), Decimal::from_str(ask)) {
                                                                let mid = (bid_d + ask_d) / Decimal::new(2, 0);
                                                                let mut prices_map = prices.write().await;
                                                                let mut bids_map = bids.write().await;
                                                                let mut asks_map = asks.write().await;
                                                                let mut updates_map = last_update.write().await;
                                                                prices_map.insert(symbol.to_string(), mid);
                                                                bids_map.insert(symbol.to_string(), bid_d);
                                                                asks_map.insert(symbol.to_string(), ask_d);
                                                                updates_map.insert(symbol.to_string(), Utc::now().timestamp_millis());
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                                tokio_tungstenite::tungstenite::Message::Close(_) => {
                                    warn!("Price feed WS closed");
                                    break;
                                }
                                _ => {}
                            }
                        }
                    }
                    Err(e) => {
                        error!("WS connection failed: {}", e);
                    }
                }
                
                // Reconnect after 5 seconds
                time::sleep(Duration::from_secs(5)).await;
            }
        });
        
        feed
    }
    
    async fn get_price(&self, symbol: &str) -> Option<Decimal> {
        self.prices.read().await.get(symbol).copied()
    }
    
    async fn is_fresh(&self, symbol: &str, max_age_ms: i64) -> bool {
        if let Some(ts) = self.last_update.read().await.get(symbol) {
            let age = Utc::now().timestamp_millis() - ts;
            age < max_age_ms
        } else {
            false
        }
    }
}

// ──────────────────────────────────────────────
// REST Client
// ──────────────────────────────────────────────

struct ValrClient {
    creds: Credentials,
    http: Client,
    base_url: String,
}

impl ValrClient {
    fn new(creds: Credentials, base_url: String) -> Self {
        Self {
            creds,
            http: Client::builder()
                .tcp_nodelay(true)
                .timeout(Duration::from_secs(10))
                .build()
                .expect("Failed to build HTTP client"),
            base_url,
        }
    }
    
    async fn get_mark_price(&self, symbol: &str) -> Result<Decimal> {
        let path = format!("/public/{}/markprice", symbol);
        let resp = self.http
            .get(format!("{}{}", self.base_url, path))
            .send()
            .await?
            .json::<serde_json::Value>()
            .await?;
        
        resp["markPrice"]
            .as_str()
            .and_then(|s| Decimal::from_str(s).ok())
            .context("Invalid mark price response")
    }
    
    async fn get_balance(&self, creds: &AccountCreds, currency: &str) -> Result<Decimal> {
        let path = "/account/balances";
        let ts = Utc::now().timestamp_millis();
        let sig_path = format!("/v1{}", path);
        let sig = creds.sign(ts, "GET", &sig_path, "");
        
        let resp = self.http
            .get(format!("{}{}", self.base_url, path))
            .header("X-VALR-API-KEY", &creds.key)
            .header("X-VALR-SIGNATURE", sig)
            .header("X-VALR-TIMESTAMP", ts.to_string())
            .send()
            .await?
            .json::<serde_json::Value>()
            .await?;
        
        if let Some(balances) = resp.as_array() {
            for bal in balances {
                if bal["currency"].as_str() == Some(currency) {
                    if let Some(available) = bal["available"].as_str() {
                        return Decimal::from_str(available)
                            .context("Failed to parse balance");
                    }
                }
            }
        }
        
        Ok(Decimal::ZERO)
    }
    
    async fn get_pair_info(&self, symbol: &str) -> Result<PairInfo> {
        // Fetch all spot pairs from VALR /v1/public/pairs/spot
        let pair_type = if symbol.ends_with("PERP") { "futures" } else { "spot" };
        let pairs_path = if pair_type == "futures" { "/public/pairs/futures" } else { "/public/pairs/spot" };
        
        let resp = self.http
            .get(format!("{}{}", self.base_url, pairs_path))
            .send()
            .await?
            .json::<serde_json::Value>()
            .await?;
        
        // Search for our symbol in the response
        // VALR API returns: { "symbol": "LINKZAR", "baseCurrency": "LINK", "quoteCurrency": "ZAR", 
        //                     "minBaseAmount": "1.0", "minQuoteAmount": "10", "tickSize": "0.01", "baseDecimalPlaces": "8" }
        if let Some(pairs) = resp.as_array() {
            for pair in pairs {
                if pair["symbol"].as_str() == Some(symbol) {
                    // tickSize = price precision (e.g., "0.01" = 2dp)
                    let tick_size = pair["tickSize"].as_str().unwrap_or("0.01");
                    let price_precision = tick_size.split('.').nth(1).map(|s| s.len() as u32).unwrap_or(2);
                    
                    let qty_precision = pair["baseDecimalPlaces"].as_u64().unwrap_or(8) as u32;
                    
                    let min_qty = pair["minBaseAmount"].as_str()
                        .and_then(|s| Decimal::from_str(s).ok())
                        .unwrap_or(Decimal::new(1, 0));
                    
                    let min_value = pair["minQuoteAmount"].as_str()
                        .and_then(|s| Decimal::from_str(s).ok())
                        .unwrap_or(Decimal::new(10, 0));
                    
                    info!("  ✅ Fetched {} from API: pp={}, qp={}, min_qty={}, min_value={}", 
                          symbol, price_precision, qty_precision, min_qty, min_value);
                    
                    return Ok(PairInfo::new(symbol, pair_type, price_precision, qty_precision, min_qty, min_value));
                }
            }
        }
        
        // Fallback to defaults if not found
        warn!("  ⚠️  {} not found in API response, using defaults", symbol);
        let defaults = if symbol.ends_with("ZAR") {
            (2, 8, dec!(1.0), dec!(10.0))
        } else {
            (2, 8, dec!(0.01), dec!(5.0))
        };
        
        Ok(PairInfo::new(symbol, pair_type, defaults.0, defaults.1, defaults.2, defaults.3))
    }
    
    async fn get_orderbook_mid(&self, symbol: &str, creds: &AccountCreds) -> Result<Decimal> {
        let path = "/orderbook";
        let ts = Utc::now().timestamp_millis();
        let body = format!("currencyPair={}", symbol);
        let sig_path = format!("/v1{}", path);
        let sig = creds.sign(ts, "GET", &sig_path, &body);
        
        let resp = self.http
            .get(format!("{}{}?{}", self.base_url, path, body))
            .header("X-VALR-API-KEY", &creds.key)
            .header("X-VALR-SIGNATURE", sig)
            .header("X-VALR-TIMESTAMP", ts.to_string())
            .send()
            .await?
            .json::<serde_json::Value>()
            .await?;
        
        let bids = resp["buyOrders"].as_array().context("No bids")?;
        let asks = resp["sellOrders"].as_array().context("No asks")?;
        
        if bids.is_empty() || asks.is_empty() {
            anyhow::bail!("Empty orderbook");
        }
        
        let bid = bids[0]["price"].as_str().and_then(|s| Decimal::from_str(s).ok())
            .context("Invalid bid price")?;
        let ask = asks[0]["price"].as_str().and_then(|s| Decimal::from_str(s).ok())
            .context("Invalid ask price")?;
        
        Ok((bid + ask) / Decimal::new(2, 0))
    }
    
    async fn place_order(
        &self,
        creds: &AccountCreds,
        symbol: &str,
        side: &str,
        quantity: Decimal,
        price: Decimal,
        ioc: bool,
        price_precision: u32,
        qty_precision: u32,
    ) -> Result<String> {
        let path = "/orders/limit";
        let ts = Utc::now().timestamp_millis();
        
        let rounded_price = price.round_dp(price_precision);
        let qty_str = format!("{:.prec$}", quantity, prec = qty_precision as usize);
        let price_str = format!("{:.prec$}", rounded_price, prec = price_precision as usize);
        
        let mut body_dict = serde_json::json!({
            "pair": symbol,
            "side": side.to_uppercase(),
            "type": "Limit",
            "quantity": qty_str,
            "price": price_str,
        });
        
        if ioc {
            body_dict["timeInForce"] = serde_json::json!("IOC");
        } else {
            body_dict["timeInForce"] = serde_json::json!("GTC");
        }
        
        let body = serde_json::to_string(&body_dict)?;
        let sig_path = format!("/v1{}", path);
        let sig = creds.sign(ts, "POST", &sig_path, &body);
        
        let resp = self.http
            .post(format!("{}{}", self.base_url, path))
            .header("X-VALR-API-KEY", &creds.key)
            .header("X-VALR-SIGNATURE", sig)
            .header("X-VALR-TIMESTAMP", ts.to_string())
            .header("Content-Type", "application/json")
            .body(body)
            .send()
            .await?;
        
        let status = resp.status();
        let result = resp.json::<serde_json::Value>().await?;
        
        if status.is_success() || status.as_u16() == 202 {
            let order_id = result["id"].as_str()
                .or_else(|| result["orderId"].as_str())
                .map(String::from)
                .context("No order ID in response")?;
            Ok(order_id)
        } else {
            anyhow::bail!("Order failed: {} - {:?}", status, result);
        }
    }
    
    async fn get_open_orders(&self, creds: &AccountCreds, symbol: &str) -> Result<Vec<serde_json::Value>> {
        let path = "/orders/open";
        let ts = Utc::now().timestamp_millis();
        let sig_path = format!("/v1{}", path);
        let sig = creds.sign(ts, "GET", &sig_path, "");
        
        let resp = self.http
            .get(format!("{}{}", self.base_url, path))
            .header("X-VALR-API-KEY", &creds.key)
            .header("X-VALR-SIGNATURE", sig)
            .header("X-VALR-TIMESTAMP", ts.to_string())
            .send()
            .await?
            .json::<serde_json::Value>()
            .await?;
        
        let orders = resp.as_array().map(|a| a.clone()).unwrap_or_default();
        Ok(orders.into_iter()
            .filter(|o| o.get("currencyPair").and_then(|v| v.as_str()) == Some(symbol))
            .collect())
    }
    
    async fn get_order_status(&self, creds: &AccountCreds, symbol: &str, order_id: &str) -> Result<serde_json::Value> {
        let path = format!("/orders/{}/{}", symbol, order_id);
        let ts = Utc::now().timestamp_millis();
        let sig_path = format!("/v1{}", path);
        let sig = creds.sign(ts, "GET", &sig_path, "");
        
        let resp = self.http
            .get(format!("{}{}", self.base_url, path))
            .header("X-VALR-API-KEY", &creds.key)
            .header("X-VALR-SIGNATURE", sig)
            .header("X-VALR-TIMESTAMP", ts.to_string())
            .send()
            .await?
            .json::<serde_json::Value>()
            .await?;
        
        Ok(resp)
    }
}

// ──────────────────────────────────────────────
// Cycle Tracker
// ──────────────────────────────────────────────

struct CycleTracker {
    state_path: String,
    maker_sides: Arc<RwLock<HashMap<String, String>>>,
    cycle_counts: Arc<RwLock<HashMap<String, u64>>>,
    account_rotation: Arc<RwLock<HashMap<String, u64>>>, // symbol -> cycle count for rotation
    maker_account: Arc<RwLock<HashMap<String, String>>>, // symbol -> "CM1" or "CM2"
}

impl CycleTracker {
    fn new(state_path: String) -> Self {
        Self {
            state_path,
            maker_sides: Arc::new(RwLock::new(HashMap::new())),
            cycle_counts: Arc::new(RwLock::new(HashMap::new())),
            account_rotation: Arc::new(RwLock::new(HashMap::new())),
            maker_account: Arc::new(RwLock::new(HashMap::new())),
        }
    }
    
    async fn load(&self) {
        if Path::new(&self.state_path).exists() {
            if let Ok(content) = fs::read_to_string(&self.state_path) {
                if let Ok(data) = serde_json::from_str::<serde_json::Value>(&content) {
                    if let Some(sides) = data["maker_sides"].as_object() {
                        let mut map = self.maker_sides.write().await;
                        for (k, v) in sides {
                            if let Some(s) = v.as_str() {
                                map.insert(k.clone(), s.to_string());
                            }
                        }
                    }
                    if let Some(counts) = data["cycle_counts"].as_object() {
                        let mut map = self.cycle_counts.write().await;
                        for (k, v) in counts {
                            if let Some(c) = v.as_u64() {
                                map.insert(k.clone(), c);
                            }
                        }
                    }
                    if let Some(rotation) = data["account_rotation"].as_object() {
                        let mut map = self.account_rotation.write().await;
                        for (k, v) in rotation {
                            if let Some(c) = v.as_u64() {
                                map.insert(k.clone(), c);
                            }
                        }
                    }
                    if let Some(accounts) = data["maker_account"].as_object() {
                        let mut map = self.maker_account.write().await;
                        for (k, v) in accounts {
                            if let Some(s) = v.as_str() {
                                map.insert(k.clone(), s.to_string());
                            }
                        }
                    }
                }
            }
        }
    }
    
    async fn get_maker_side(&self, symbol: &str) -> String {
        if let Some(side) = self.maker_sides.read().await.get(symbol) {
            side.clone()
        } else {
            "BUY".to_string()
        }
    }
    
    async fn get_maker_account(&self, symbol: &str) -> String {
        // Default to CM1, rotate to CM2 every 3 cycles
        let mut rotation = self.account_rotation.write().await;
        let count = rotation.entry(symbol.to_string()).or_insert(0);
        *count += 1;
        
        // Rotate every 3 cycles
        let maker_account = if (*count - 1) / 3 % 2 == 0 { "CM1" } else { "CM2" };
        
        let mut accounts = self.maker_account.write().await;
        accounts.insert(symbol.to_string(), maker_account.to_string());
        
        maker_account.to_string()
    }
    
    async fn record_cycle(&self, symbol: &str, maker_side: &str) {
        // Toggle maker side for next cycle
        let next_side = if maker_side == "BUY" { "SELL" } else { "BUY" };
        self.maker_sides.write().await.insert(symbol.to_string(), next_side.to_string());
        
        // Increment cycle count
        {
            let mut counts = self.cycle_counts.write().await;
            let count = counts.entry(symbol.to_string()).or_insert(0);
            *count += 1;
        }
    }
    
    async fn save(&self) -> Result<()> {
        let maker_sides = self.maker_sides.read().await.clone();
        let cycle_counts = self.cycle_counts.read().await.clone();
        let account_rotation = self.account_rotation.read().await.clone();
        let maker_account = self.maker_account.read().await.clone();
        
        let data = serde_json::json!({
            "maker_sides": maker_sides,
            "cycle_counts": cycle_counts,
            "account_rotation": account_rotation,
            "maker_account": maker_account,
        });
        
        fs::write(&self.state_path, serde_json::to_string_pretty(&data)?)?;
        Ok(())
    }
}

// ──────────────────────────────────────────────
// Chart Maintenance Bot
// ──────────────────────────────────────────────

struct CMBot {
    config: Config,
    creds: Credentials,
    client: ValrClient,
    price_feed: PriceFeed,
    cycle_tracker: CycleTracker,
    pairs: HashMap<String, PairInfo>,
}

impl CMBot {
    fn new(config: Config, creds: Credentials) -> Self {
        let base_url = config.api_base_url.clone().unwrap_or_else(|| "https://api.valr.com/v1".to_string());
        let client = ValrClient::new(creds.clone(), base_url);
        let price_feed = PriceFeed::new();
        let cycle_tracker = CycleTracker::new("state.json".to_string());
        
        Self {
            config,
            creds,
            client,
            price_feed,
            cycle_tracker,
            pairs: HashMap::new(),
        }
    }
    
    async fn init_pairs(&mut self, symbols: &[String]) {
        info!("Fetching pair info from VALR API...");
        for symbol in symbols {
            match self.client.get_pair_info(symbol).await {
                Ok(pair_info) => {
                    info!("  ✅ {}: min_qty={}, min_value={}, price_precision={}, qty_precision={}", 
                          symbol, pair_info.min_qty, pair_info.min_value, pair_info.price_precision, pair_info.qty_precision);
                    self.pairs.insert(symbol.clone(), pair_info);
                }
                Err(e) => {
                    error!("  ❌ Failed to fetch info for {}: {}", symbol, e);
                    // Use fallback values
                    let fallback = PairInfo::new(symbol, "spot", 2, 8, dec!(1.0), dec!(10.0));
                    self.pairs.insert(symbol.clone(), fallback);
                }
            }
        }
    }
    
    async fn get_price(&self, symbol: &str) -> Result<Decimal> {
        // Try WebSocket first
        if let Some(price) = self.price_feed.get_price(symbol).await {
            if self.price_feed.is_fresh(symbol, 5000).await {
                return Ok(price);
            }
        }
        
        // Fallback to REST mark price
        self.client.get_mark_price(symbol).await
    }
    
    async fn execute_cycle(&self, symbol: &str) -> Result<bool> {
        let pair_info = self.pairs.get(symbol).context("Unknown pair")?;
        
        // Get maker side (rotates between BUY/SELL)
        let maker_side = self.cycle_tracker.get_maker_side(symbol).await;
        let taker_side = if maker_side == "BUY" { "SELL" } else { "BUY" };
        
        // Get mark price from REST
        let mark_price = self.client.get_mark_price(symbol).await?;
        
        // Get mid price from WebSocket orderbook (preferred) or fallback to REST
        let mid_price = if let (Some(bid), Some(ask)) = (
            self.price_feed.get_bid(symbol).await,
            self.price_feed.get_ask(symbol).await,
        ) {
            if self.price_feed.is_fresh(symbol, 5000).await {
                (bid + ask) / Decimal::new(2, 0)
            } else {
                // WebSocket stale, use REST fallback
                self.client.get_orderbook_mid(symbol, &self.creds.cm1).await.unwrap_or(mark_price)
            }
        } else {
            // No WebSocket data, use REST fallback
            self.client.get_orderbook_mid(symbol, &self.creds.cm1).await.unwrap_or(mark_price)
        };
        
        // Average of mid + mark
        let price = (mark_price + mid_price) / Decimal::new(2, 0);
        let price = price.round_dp(pair_info.price_precision);
        
        // Get maker account (rotates every 3 cycles between CM1 and CM2)
        let maker_account = self.cycle_tracker.get_maker_account(symbol).await;
        let taker_account = if maker_account == "CM1" { "CM2" } else { "CM1" };
        
        // Get balance limits for maker account
        let maker_creds = if maker_account == "CM1" { &self.creds.cm1 } else { &self.creds.cm2 };
        let base_asset = if symbol.ends_with("ZAR") {
            symbol.replace("ZAR", "")
        } else if symbol.ends_with("USDTPERP") {
            symbol.replace("USDTPERP", "")
        } else if symbol.ends_with("USDT") {
            symbol.replace("USDT", "")
        } else {
            symbol.to_string()
        };
        let quote_asset = if symbol.ends_with("PERP") || symbol.ends_with("USDT") { "USDT" } else { "ZAR" };
        
        // Check balance for maker order
        let maker_balance = if maker_side == "BUY" {
            // Need quote currency (ZAR/USDT)
            self.client.get_balance(maker_creds, quote_asset).await.unwrap_or(Decimal::MAX)
        } else {
            // Need base currency (LINK, BTC, etc)
            self.client.get_balance(maker_creds, &base_asset).await.unwrap_or(Decimal::MAX)
        };
        
        // Calculate max quantity based on balance
        let max_qty_from_balance = if maker_side == "BUY" {
            maker_balance / price
        } else {
            maker_balance
        };
        
        // Generate random quantity
        let mut rng = rand::thread_rng();
        let multiplier = rng.gen_range(self.config.qty_range_min_multiplier..=self.config.qty_range_max_multiplier);
        let raw_qty = pair_info.min_qty * Decimal::from_f64_retain(multiplier).unwrap();
        
        // Round UP to ensure >= minimum
        let precision_factor = Decimal::new(10_i64.pow(pair_info.qty_precision), 0);
        let requested_qty = (raw_qty * precision_factor).ceil() / precision_factor;
        
        // Ensure minimum value (from API) is met
        let min_value_qty = pair_info.min_value / price;
        let min_value_qty = (min_value_qty * precision_factor).ceil() / precision_factor;
        let requested_qty = requested_qty.max(min_value_qty);
        let requested_qty = requested_qty.max(pair_info.min_qty);
        
        // Cap quantity to 80% of available balance (leave buffer)
        let safe_qty = max_qty_from_balance * Decimal::from_f64_retain(0.8).unwrap();
        
        // Determine final quantity
        let qty = if requested_qty <= safe_qty {
            // Balance sufficient for requested amount
            requested_qty
        } else if pair_info.min_qty <= safe_qty && min_value_qty <= safe_qty {
            // Balance insufficient for requested, but can afford minimum
            let fallback_qty = pair_info.min_qty.max(min_value_qty);
            warn!("⚠️  Balance insufficient for requested {}, falling back to min {}", requested_qty, fallback_qty);
            fallback_qty
        } else {
            // Balance too low even for minimum - SKIP this cycle
            error!("❌ INSUFFICIENT BALANCE: requested={}, safe={}, min_qty={}, min_value={}. Skipping cycle.", 
                   requested_qty, safe_qty, pair_info.min_qty, pair_info.min_value);
            return Ok(false);
        };
        
        info!("=== CYCLE: {} ===", symbol);
        info!("  Maker: {} ({}) | Taker: {} ({})", maker_account, maker_side, taker_account, taker_side);
        info!("  Qty: {} (min={}, mult={:.3})", qty, pair_info.min_qty, multiplier);
        info!("  Price: {} (mid+mark avg)", price);
        
        // Place maker order (GTC) on designated account
        let maker_creds = if maker_account == "CM1" { &self.creds.cm1 } else { &self.creds.cm2 };
        let maker_result = self.client.place_order(
            maker_creds,
            symbol,
            &maker_side,
            qty,
            price,
            false, // GTC
            pair_info.price_precision,
            pair_info.qty_precision,
        ).await;
        
        let maker_order_id = match maker_result {
            Ok(id) => {
                info!("  ✅ Maker {} @ {} → {}", maker_side, price, &id[..8]);
                id
            }
            Err(e) => {
                error!("  ❌ Maker order failed: {}", e);
                return Ok(false);
            }
        };
        
        // Wait 5ms then place taker order (IOC) at SAME price as maker
        // This ensures maker order has time to rest on book and get queue priority
        time::sleep(Duration::from_millis(5)).await;
        
        // Taker at exact same price as maker - will hit our resting order
        let taker_price = price;
        
        info!("  Placing taker IOC {} @ {} (maker was {} @ {})...", taker_side, taker_price, maker_side, price);
        
        let taker_creds = if taker_account == "CM1" { &self.creds.cm1 } else { &self.creds.cm2 };
        let taker_result = self.client.place_order(
            taker_creds,
            symbol,
            &taker_side,
            qty,
            taker_price,
            true, // IOC
            pair_info.price_precision,
            pair_info.qty_precision,
        ).await;
        
        let taker_order_id = match taker_result {
            Ok(id) => {
                info!("  ✅ Taker {} @ {} → {}", taker_side, taker_price, &id[..8]);
                id
            }
            Err(e) => {
                error!("  ❌ Taker order FAILED: {}", e);
                // Check if maker is still open
                let maker_still_open = self.client.get_open_orders(maker_creds, symbol).await
                    .map(|orders| orders.iter().any(|o| o["orderId"].as_str() == Some(&maker_order_id)))
                    .unwrap_or(false);
                if maker_still_open {
                    let _ = self.cancel_order(maker_creds, symbol, &maker_order_id).await;
                    info!("  Cancelled resting maker order");
                }
                return Ok(false);
            }
        };
        
        // Wait for fills
        time::sleep(Duration::from_millis(500)).await;
        
        // Check orders on both accounts
        let cm1_orders = self.client.get_open_orders(&self.creds.cm1, symbol).await.unwrap_or_default();
        let cm2_orders = self.client.get_open_orders(&self.creds.cm2, symbol).await.unwrap_or_default();
        
        let maker_open = if maker_account == "CM1" {
            cm1_orders.iter().any(|o| o["orderId"].as_str() == Some(&maker_order_id))
        } else {
            cm2_orders.iter().any(|o| o["orderId"].as_str() == Some(&maker_order_id))
        };
        
        let taker_open = if taker_account == "CM1" {
            cm1_orders.iter().any(|o| o["orderId"].as_str() == Some(&taker_order_id))
        } else {
            cm2_orders.iter().any(|o| o["orderId"].as_str() == Some(&taker_order_id))
        };
        
        // Get detailed order status
        let maker_status = self.client.get_order_status(maker_creds, symbol, &maker_order_id).await;
        let taker_status = self.client.get_order_status(taker_creds, symbol, &taker_order_id).await;
        
        let maker_filled_qty = maker_status.as_ref()
            .ok()
            .and_then(|m| m["filledQuantity"].as_str())
            .and_then(|s| Decimal::from_str(s).ok())
            .unwrap_or(Decimal::ZERO);
        
        let taker_filled_qty = taker_status.as_ref()
            .ok()
            .and_then(|m| m["filledQuantity"].as_str())
            .and_then(|s| Decimal::from_str(s).ok())
            .unwrap_or(Decimal::ZERO);
        
        info!("  Maker: open={}, filled_qty={}", maker_open, maker_filled_qty);
        info!("  Taker: open={}, filled_qty={}", taker_open, taker_filled_qty);
        
        let maker_filled = !maker_open || maker_filled_qty > Decimal::ZERO;
        let taker_filled = !taker_open || taker_filled_qty > Decimal::ZERO;
        
        if maker_filled && taker_filled {
            if maker_open && taker_open {
                // Both still open but have fills - partial fill scenario
                info!("  ✅ Cycle COMPLETE: Both orders filled (maker={}, taker={})", maker_filled_qty, taker_filled_qty);
            } else if !maker_open && !taker_open {
                info!("  ✅ Cycle COMPLETE: Both orders fully filled");
            } else if !maker_open {
                info!("  ✅ Cycle COMPLETE: Maker filled, taker filled {}", taker_filled_qty);
            } else {
                info!("  ✅ Cycle COMPLETE: Taker filled, maker filled {}", maker_filled_qty);
            }
            self.cycle_tracker.record_cycle(symbol, &maker_side).await;
            self.cycle_tracker.save().await?;
            Ok(true)
        } else if taker_filled && maker_open && maker_filled_qty == Decimal::ZERO {
            // Taker filled but maker has ZERO fills = external fill
            warn!("  ⚠️  EXTERNAL FILL: Taker filled {} but maker has 0 fills", taker_filled_qty);
            warn!("     Taker hit external liquidity, maker {} still resting", &maker_order_id[..8]);
            self.cycle_tracker.record_cycle(symbol, &maker_side).await;
            self.cycle_tracker.save().await?;
            Ok(true)
        } else if maker_filled && taker_open && taker_filled_qty == Decimal::ZERO {
            warn!("  ⚠️  Maker filled, taker still open with 0 fills");
            self.cycle_tracker.record_cycle(symbol, &maker_side).await;
            self.cycle_tracker.save().await?;
            Ok(true)
        } else {
            error!("  ❌ Cycle INCOMPLETE: Orders still open, no fills");
            Ok(false)
        }
    }
    
    async fn cancel_order(&self, creds: &AccountCreds, symbol: &str, order_id: &str) -> Result<()> {
        let path = format!("/orders/{}/{}", symbol, order_id);
        let ts = Utc::now().timestamp_millis();
        let sig_path = format!("/v1{}", path);
        let sig = creds.sign(ts, "DELETE", &sig_path, "");
        
        self.client.http
            .delete(format!("{}{}", self.client.base_url, path))
            .header("X-VALR-API-KEY", &creds.key)
            .header("X-VALR-SIGNATURE", sig)
            .header("X-VALR-TIMESTAMP", ts.to_string())
            .send()
            .await?;
        
        Ok(())
    }
    
    async fn run(&mut self, mode: &str) -> Result<()> {
        // Load cycle tracker state
        self.cycle_tracker.load().await;
        
        // Determine pairs based on mode
        let symbols = match mode {
            "test" => vec![self.config.test_pair.clone()],
            "test_spot" => vec![self.config.test_spot_pair.clone().unwrap_or_else(|| "BTCZAR".to_string())],
            "phase1" => self.config.phase1_pairs.clone(),
            "phase2" => self.config.phase2_pairs_enabled.clone().unwrap_or_default(),
            _ => self.config.phase1_pairs.clone(),
        };
        
        info!("=== CHART MAINTENANCE BOT (Rust) ===");
        info!("Mode: {} | Pairs: {:?}", mode, symbols);
        
        // Initialize pairs (fetch from API)
        self.init_pairs(&symbols).await;
        
        // Start WebSocket price feed
        self.price_feed = PriceFeed::spawn_listener(symbols.clone()).await;
        
        // Wait for initial prices
        info!("Waiting for price updates...");
        time::sleep(Duration::from_secs(3)).await;
        
        // Main loop
        let mut interval = time::interval(Duration::from_secs(self.config.cycle_interval_seconds));
        
        loop {
            interval.tick().await;
            
            for symbol in &symbols {
                if let Err(e) = self.execute_cycle(symbol).await {
                    error!("Cycle failed for {}: {}", symbol, e);
                }
                
                // Small delay between pairs
                time::sleep(Duration::from_millis(100)).await;
            }
        }
    }
}

// ──────────────────────────────────────────────
// Main
// ──────────────────────────────────────────────

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_target(false)
        .with_level(true)
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive("cm_bot=info".parse()?)
        )
        .init();
    
    info!("🤖 Chart Maintenance Bot (Rust) starting...");
    
    // Load config
    let config_path = "config.json";
    let config: Config = if Path::new(config_path).exists() {
        let content = fs::read_to_string(config_path)?;
        serde_json::from_str(&content).unwrap_or_default()
    } else {
        Config::default()
    };
    
    // Load credentials
    let creds = Credentials::load()?;
    
    // Parse command line args
    let args: Vec<String> = std::env::args().collect();
    let mode = if args.iter().any(|a| a == "--phase1") {
        "phase1"
    } else if args.iter().any(|a| a == "--phase2") {
        "phase2"
    } else if args.iter().any(|a| a == "--test-spot") {
        "test_spot"
    } else if args.iter().any(|a| a == "--test") {
        "test"
    } else {
        "phase1"
    };
    
    // Run bot
    let mut bot = CMBot::new(config, creds);
    bot.run(mode).await?;
    
    Ok(())
}
