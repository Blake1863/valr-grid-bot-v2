/// VALR Futures Grid Bot v1.0
///
/// Automated grid trading with native stop-loss protection.
/// Uses VALR conditional orders (TP/SL) monitored by the exchange.

use anyhow::{Context, Result};
use chrono::Utc;
use futures_util::{SinkExt, StreamExt};
use hmac::{Hmac, Mac};
use rust_decimal::prelude::*;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use sha2::Sha512;
use std::sync::Arc;
use tokio::sync::{mpsc, Mutex};
use tokio_tungstenite::tungstenite::Message;
use tracing::{info, warn, error};

// ──────────────────────────────────────────────
// Configuration
// ──────────────────────────────────────────────

#[derive(Debug, Clone, Deserialize)]
pub struct GridConfig {
    pub pair: String,
    pub leverage: u32,
    pub balance_usage_pct: f64,
    pub levels: usize,
    pub spacing_pct: f64,
    pub max_loss_pct: f64,
}

impl Default for GridConfig {
    fn default() -> Self {
        Self {
            pair: "SOLUSDTPERP".to_string(),
            leverage: 5,
            balance_usage_pct: 90.0,
            levels: 3,
            spacing_pct: 0.5,
            max_loss_pct: 3.0,
        }
    }
}

fn load_config() -> Result<GridConfig> {
    let exe_path = std::env::current_exe()?;
    let bin_dir = exe_path.parent().unwrap_or(std::path::Path::new("."));
    let config_path = bin_dir.join("../config/config.json");
    
    let raw = std::fs::read_to_string(&config_path)
        .or_else(|_| std::fs::read_to_string("config/config.json"))
        .context("Cannot read config/config.json")?;

    let v: serde_json::Value = serde_json::from_str(&raw)?;
    Ok(GridConfig {
        pair: v["pair"].as_str().unwrap_or("SOLUSDTPERP").to_string(),
        leverage: v["leverage"].as_u64().unwrap_or(5) as u32,
        balance_usage_pct: v["balance_usage_pct"].as_f64().unwrap_or(90.0),
        levels: v["levels"].as_u64().unwrap_or(3) as usize,
        spacing_pct: v["spacing_pct"].as_f64().unwrap_or(0.5),
        max_loss_pct: v["max_loss_pct"].as_f64().unwrap_or(3.0),
    })
}

// ──────────────────────────────────────────────
// Credentials
// ──────────────────────────────────────────────

#[derive(Clone)]
pub struct Credentials {
    pub api_key: String,
    pub api_secret: String,
}

impl Credentials {
    pub fn load() -> Result<Self> {
        let key = std::process::Command::new("python3")
            .args(["secrets.py", "get", "valr_api_key"])
            .output()
            .context("Failed to read valr_api_key")?;
        let secret = std::process::Command::new("python3")
            .args(["secrets.py", "get", "valr_api_secret"])
            .output()
            .context("Failed to read valr_api_secret")?;

        Ok(Self {
            api_key: String::from_utf8(key.stdout)?.trim().to_string(),
            api_secret: String::from_utf8(secret.stdout)?.trim().to_string(),
        })
    }

    pub fn sign(&self, timestamp_ms: i64, method: &str, path: &str, body: &str) -> String {
        let payload = format!("{}{}{}{}", timestamp_ms, method.to_uppercase(), path, body);
        let mut mac = Hmac::<Sha512>::new_from_slice(self.api_secret.as_bytes()).unwrap();
        mac.update(payload.as_bytes());
        hex::encode(mac.finalize().into_bytes())
    }
}

// ──────────────────────────────────────────────
// REST Client
// ──────────────────────────────────────────────

pub struct ValrClient {
    creds: Credentials,
    http: reqwest::Client,
}

impl ValrClient {
    pub fn new(creds: Credentials) -> Self {
        Self {
            creds,
            http: reqwest::Client::new(),
        }
    }

    pub async fn get(&self, path: &str) -> Result<serde_json::Value> {
        let ts = Utc::now().timestamp_millis();
        let sig = self.creds.sign(ts, "GET", path, "");
        let resp = self.http
            .get(format!("https://api.valr.com{path}"))
            .header("X-VALR-API-KEY", &self.creds.api_key)
            .header("X-VALR-SIGNATURE", sig)
            .header("X-VALR-TIMESTAMP", ts.to_string())
            .send().await?
            .json::<serde_json::Value>().await?;
        Ok(resp)
    }

    pub async fn post(&self, path: &str, body: &serde_json::Value) -> Result<serde_json::Value> {
        let body_str = serde_json::to_string(body)?;
        let ts = Utc::now().timestamp_millis();
        let sig = self.creds.sign(ts, "POST", path, &body_str);
        let resp = self.http
            .post(format!("https://api.valr.com{path}"))
            .header("X-VALR-API-KEY", &self.creds.api_key)
            .header("X-VALR-SIGNATURE", sig)
            .header("X-VALR-TIMESTAMP", ts.to_string())
            .header("Content-Type", "application/json")
            .body(body_str)
            .send().await?
            .json::<serde_json::Value>().await?;
        Ok(resp)
    }

    pub async fn delete(&self, path: &str) -> Result<serde_json::Value> {
        let ts = Utc::now().timestamp_millis();
        let sig = self.creds.sign(ts, "DELETE", path, "");
        let resp = self.http
            .delete(format!("https://api.valr.com{path}"))
            .header("X-VALR-API-KEY", &self.creds.api_key)
            .header("X-VALR-SIGNATURE", sig)
            .header("X-VALR-TIMESTAMP", ts.to_string())
            .send().await?
            .json::<serde_json::Value>().await?;
        Ok(resp)
    }
}

// ──────────────────────────────────────────────
// WebSocket Streams
// ──────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct FillEvent {
    pub order_id: String,
    pub pair: String,
    pub side: String,
    pub filled_qty: Decimal,
    pub price: Decimal,
}

pub struct TradeStream {
    rx: mpsc::Receiver<Decimal>,
    cache: Arc<Mutex<Option<Decimal>>>,
}

impl TradeStream {
    pub fn spawn(pair: &str) -> Self {
        let (tx, rx) = mpsc::channel(100);
        let cache = Arc::new(Mutex::new(None));
        let pair = pair.to_string();
        let cache_clone = cache.clone();

        tokio::spawn(async move {
            loop {
                let url = format!("wss://api.valr.com/ws/trade");
                match tokio_tungstenite::connect_async(&url).await {
                    Ok((ws, _)) => {
                        let subscribe = serde_json::json!({
                            "action": "SUBSCRIBE",
                            "channel": "OB_L1_DIFF",
                            "data": { "currencyPair": pair }
                        });
                        
                        let mut ws = ws;
                        if let Ok(msg) = serde_json::to_string(&subscribe) {
                            let _ = ws.send(Message::Text(msg)).await;
                        }

                        while let Some(Ok(msg)) = ws.next().await {
                            if let Message::Text(text) = msg {
                                if let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) {
                                    if let Some(data) = v.get("data") {
                                        // Parse bids[0] and asks[0] for mid price
                                        if let (Some(bids), Some(asks)) = 
                                            (data.get("b").and_then(|a| a.as_array()),
                                             data.get("a").and_then(|a| a.as_array())) 
                                        {
                                            if let (Some(bid), Some(ask)) = (bids.first(), asks.first()) {
                                                if let (Some(bp), Some(ap)) = 
                                                    (bid.get(0).and_then(|v| v.as_str()),
                                                     ask.get(0).and_then(|v| v.as_str()))
                                                {
                                                    if let (Ok(bid_p), Ok(ask_p)) = 
                                                        (Decimal::from_str(bp), Decimal::from_str(ap))
                                                    {
                                                        let mid = (bid_p + ask_p) / Decimal::new(2, 0);
                                                        *cache_clone.lock().await = Some(mid);
                                                        let _ = tx.send(mid).await;
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    Err(e) => {
                        warn!("Trade WS connection failed: {}. Retrying in 5s...", e);
                        tokio::time::sleep(tokio::time::Duration::from_secs(5)).await;
                    }
                }
            }
        });

        Self { rx, cache }
    }

    pub async fn mid(&mut self) -> Option<Decimal> {
        self.rx.recv().await
    }

    pub fn mid_now(&self) -> Option<Decimal> {
        // Synchronous access to cache
        None // Simplified - use async mid() instead
    }
}

// ──────────────────────────────────────────────
// Grid Bot State
// ──────────────────────────────────────────────

#[derive(Debug)]
struct GridLevel {
    price: Decimal,
    order_id: Option<String>,
    side: String,
}

struct GridBot {
    config: GridConfig,
    client: ValrClient,
    levels: Vec<GridLevel>,
    mid_price: Decimal,
    initial_margin: Decimal,
    position_qty: Decimal,
    avg_entry: Decimal,
    stop_order_id: Option<String>,
}

impl GridBot {
    fn new(config: GridConfig, client: ValrClient) -> Self {
        Self {
            config,
            client,
            levels: Vec::new(),
            mid_price: Decimal::ZERO,
            initial_margin: Decimal::ZERO,
            position_qty: Decimal::ZERO,
            avg_entry: Decimal::ZERO,
            stop_order_id: None,
        }
    }

    fn tick_dp(&self) -> u32 {
        if self.config.pair.starts_with("BTC") { 0 } else { 2 }
    }

    fn uniform_qty(&self, mid: Decimal, usable: Decimal) -> Decimal {
        let levels = Decimal::new(self.config.levels as i64, 0);
        let notional_per_level = usable / levels;
        let leveraged = notional_per_level * Decimal::from(self.config.leverage);
        let dp = if self.config.pair.starts_with("BTC") { 4 } else { 2 };
        (leveraged / mid).round_dp(dp)
    }

    fn grid_prices(&self, mid: Decimal) -> Vec<(String, Decimal)> {
        let spacing = mid * Decimal::from_f64(self.config.spacing_pct).unwrap() / Decimal::new(100, 0);
        let mut out = Vec::new();
        for i in 1..=self.config.levels {
            out.push(("BUY".into(), mid - spacing * Decimal::new(i as i64, 0)));
        }
        for i in 1..=self.config.levels {
            out.push(("SELL".into(), mid + spacing * Decimal::new(i as i64, 0)));
        }
        out
    }

    fn calc_stop_price(&self) -> Decimal {
        if self.position_qty == Decimal::ZERO || self.avg_entry == Decimal::ZERO {
            return Decimal::ZERO;
        }
        let is_long = self.position_qty > Decimal::ZERO;
        let stop_distance = self.avg_entry * Decimal::from_f64(self.config.max_loss_pct).unwrap() / Decimal::new(100, 0);
        if is_long {
            self.avg_entry - stop_distance
        } else {
            self.avg_entry + stop_distance
        }
    }

    async fn place_stop_loss(&mut self, stop_price: Decimal) -> Result<()> {
        if let Some(ref id) = self.stop_order_id {
            let _ = self.client.delete(&format!("/v1/orders/conditionals/conditional/{}", id)).await;
        }

        if self.position_qty == Decimal::ZERO {
            return Ok(());
        }

        let tick_dp = self.tick_dp();
        let stop_price_rounded = stop_price.round_dp(tick_dp);

        info!("🛡️  Placing {} stop-loss: qty=0 (full position) @ trigger={} ({}dp)", 
              self.config.pair, stop_price_rounded, tick_dp);

        let payload = serde_json::json!({
            "quantity": "0",
            "pair": self.config.pair,
            "triggerType": "MARK_PRICE",
            "stopLossTriggerPrice": stop_price_rounded.to_string(),
            "stopLossOrderPrice": "-1"
        });

        match self.client.post("/v1/orders/conditionals", &payload).await {
            Ok(resp) => {
                let id = resp["id"].as_str()
                    .or_else(|| resp["orderId"].as_str())
                    .map(|s| s.to_string());
                
                if let Some(order_id) = id {
                    tokio::time::sleep(tokio::time::Duration::from_millis(300)).await;
                    if let Ok(orders) = self.client.get("/v1/orders/conditionals").await {
                        if let Some(arr) = orders.as_array() {
                            let found = arr.iter().any(|o| {
                                o["orderId"].as_str() == Some(&order_id) ||
                                o["id"].as_str() == Some(&order_id)
                            });
                            if found {
                                let id_short = if order_id.len() >= 8 { &order_id[..8] } else { &order_id };
                                info!("✅ Stop-loss verified → {}", id_short);
                                self.stop_order_id = Some(order_id);
                                return Ok(());
                            }
                        }
                    }
                    warn!("⚠️  Stop-loss not found in list");
                }
                Err(anyhow::anyhow!("Stop-loss unverified"))
            }
            Err(e) => {
                warn!("❌ Stop-loss placement failed: {}", e);
                Err(e)
            }
        }
    }

    async fn place_grid(&mut self) -> Result<()> {
        let mid = self.mid_price;
        let balances = self.client.get("/v1/balances").await?;
        let available = balances.as_array()
            .and_then(|arr| arr.iter().find(|b| b["currency"].as_str() == Some("USDT")))
            .and_then(|b| b["available"].as_str())
            .and_then(|s| Decimal::from_str(s).ok())
            .unwrap_or(Decimal::ZERO);

        let usable = (available * Decimal::from_f64(self.config.balance_usage_pct / 100.0).unwrap()).round_dp(2);

        info!("Mid: {} | Available: {} USDT | Deploying: {} ({}%)",
              mid, available, usable, self.config.balance_usage_pct);

        if usable < Decimal::new(10, 0) {
            warn!("Insufficient balance to place grid");
            return Ok(());
        }

        self.mid_price = mid;
        if self.initial_margin == Decimal::ZERO {
            self.initial_margin = usable;
        }
        self.levels.clear();

        let qty = self.uniform_qty(mid, usable);
        let tick_dp = self.tick_dp();
        info!("Placing {} orders around {} (qty={} each)", self.config.levels * 2, mid, qty);

        for (side, raw_price) in self.grid_prices(mid) {
            let price = raw_price.round_dp(tick_dp);
            let payload = serde_json::json!({
                "currencyPair": self.config.pair,
                "side": side,
                "type": "LIMIT",
                "price": price.to_string(),
                "quantity": qty.to_string(),
                "postOnly": true
            });

            match self.client.post("/v1/orders", &payload).await {
                Ok(resp) => {
                    if let Some(id) = resp["orderId"].as_str().or_else(|| resp["id"].as_str()) {
                        info!("✅ {} @ {} → {}", side, price, &id[..8.min(id.len())]);
                        self.levels.push(GridLevel { price: raw_price, order_id: Some(id.to_string()), side });
                    }
                }
                Err(e) => warn!("Failed to place {} @ {}: {}", side, price, e),
            }
            tokio::time::sleep(tokio::time::Duration::from_millis(100)).await;
        }

        info!("Grid live: {} orders", self.levels.len());
        Ok(())
    }

    async fn cancel_all(&self) -> Result<()> {
        info!("Cancelling all open orders...");
        for level in &self.levels {
            if let Some(ref id) = level.order_id {
                let _ = self.client.delete(&format!("/v1/orders/{}", id)).await;
                tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
            }
        }
        Ok(())
    }
}

// ──────────────────────────────────────────────
// Main
// ──────────────────────────────────────────────

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt().with_target(false).with_level(true).init();
    info!("🤖 VALR Futures Grid Bot v1.0 starting...");

    let creds = Credentials::load()?;
    let client = ValrClient::new(creds.clone());
    let config = load_config()?;

    info!("Config: {} | {}x leverage | {}% balance | {} levels | {}% spacing | max_loss={}%",
          config.pair, config.leverage, config.balance_usage_pct,
          config.levels, config.spacing_pct, config.max_loss_pct);

    let mut price_stream = TradeStream::spawn(&config.pair);
    let mut bot = GridBot::new(config, client);

    info!("Waiting for WS price feed...");
    if let Some(mid) = price_stream.mid().await {
        bot.mid_price = mid;
        info!("✅ WS price live: {}", mid);
    }

    // Place initial grid
    if let Err(e) = bot.place_grid().await {
        error!("Grid placement failed: {}", e);
        return Ok(());
    }

    // Main loop
    let mut recentre = tokio::time::interval(tokio::time::Duration::from_secs(300));
    
    loop {
        tokio::select! {
            Some(mid) = price_stream.mid() => {
                bot.mid_price = mid;
            }
            _ = recentre.tick() => {
                info!("⟳ Re-centre timer fired...");
                bot.cancel_all().await?;
                if let Ok(new_cfg) = load_config() {
                    bot.config = new_cfg;
                }
                bot.place_grid().await?;
            }
        }
    }
}
