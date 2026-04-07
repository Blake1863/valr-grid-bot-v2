use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;
use anyhow::Result;
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;

#[derive(Clone)]
pub struct PriceFeed {
    prices: Arc<RwLock<HashMap<String, OrderbookPrices>>>,
    ws_url: String,
}

#[derive(Debug, Clone)]
pub struct OrderbookPrices {
    pub bid: f64,
    pub ask: f64,
    pub mid: f64,
    pub mark_price: Option<f64>, // from MARKET_SUMMARY_UPDATE, used when spread > 50bps
}

#[derive(Debug, Deserialize)]
struct WsResponse {
    #[serde(rename = "type")]
    msg_type: String,
    #[serde(default, rename = "currencyPairSymbol")]
    currency_pair_symbol: Option<String>,
    #[serde(default)]
    data: Option<serde_json::Value>,
    #[serde(default, rename = "payload")]
    payload: Option<serde_json::Value>,
}

#[derive(Debug, Deserialize)]
struct OrderbookData {
    Bids: Option<Vec<OrderbookEntry>>,
    Asks: Option<Vec<OrderbookEntry>>,
}

#[derive(Debug, Deserialize)]
struct OrderbookEntry {
    price: String,
    #[serde(default)]
    quantity: Option<String>,
    #[serde(default)]
    orderCount: Option<u64>,
}

#[derive(Debug, Deserialize)]
struct MarketSummaryData {
    #[serde(default, rename = "lastTradedPrice")]
    last_traded_price: Option<String>,
    #[serde(default, rename = "markPrice")]
    mark_price: Option<String>,
}

impl PriceFeed {
    pub fn new(ws_url: &str) -> Self {
        Self {
            prices: Arc::new(RwLock::new(HashMap::new())),
            ws_url: ws_url.to_string(),
        }
    }

    pub async fn connect_and_subscribe(&self, symbols: &[&str]) -> Result<()> {
        let ws_url = self.ws_url.clone();
        let prices = self.prices.clone();
        let symbols: Vec<String> = symbols.iter().map(|s| s.to_string()).collect();

        tokio::spawn(async move {
            loop {
                match run_price_ws_loop(&ws_url, &symbols, prices.clone()).await {
                    Ok(_) => println!("[WARN] Trade WS connection closed, reconnecting in 3s..."),
                    Err(e) => eprintln!("[ERROR] Trade WS error: {} - reconnecting in 3s...", e),
                }
                tokio::time::sleep(tokio::time::Duration::from_secs(3)).await;
            }
        });

        Ok(())
    }

    pub async fn get_orderbook(&self, symbol: &str) -> Option<OrderbookPrices> {
        let prices = self.prices.read().await;
        prices.get(symbol).cloned()
    }

    // Backwards compat
    pub async fn get_price(&self, symbol: &str) -> Option<f64> {
        self.get_orderbook(symbol).await.map(|p| p.mid)
    }

    pub async fn update_price(&self, symbol: &str, price: f64) {
        let mut prices = self.prices.write().await;
        prices.insert(symbol.to_string(), OrderbookPrices { bid: price, ask: price, mid: price, mark_price: None });
    }
}

/// Main WebSocket loop for price feed - connects, subscribes, and listens for orderbook updates
async fn run_price_ws_loop(
    ws_url: &str,
    symbols: &[String],
    prices: Arc<RwLock<HashMap<String, OrderbookPrices>>>,
) -> Result<()> {
    // Connect to WebSocket (no auth required for public trade stream)
    let (ws_stream, _response) = tokio_tungstenite::connect_async(ws_url)
        .await
        .map_err(|e| anyhow::anyhow!("Failed to connect to {}: {}", ws_url, e))?;

    println!("[INFO] Trade WS connected, subscribed to {} pairs", symbols.len());

    let (mut write, mut read) = ws_stream.split::<tokio_tungstenite::tungstenite::Message>();

    // Subscribe to AGGREGATED_ORDERBOOK_UPDATE
    let subscribe_msg = serde_json::json!({
        "type": "SUBSCRIBE",
        "subscriptions": [
            {
                "event": "AGGREGATED_ORDERBOOK_UPDATE",
                "pairs": symbols
            }
        ]
    });

    let subscribe_json = serde_json::to_string(&subscribe_msg)?;
    write.send(tokio_tungstenite::tungstenite::Message::Text(subscribe_json.into())).await
        .map_err(|e| anyhow::anyhow!("Failed to send subscribe message: {}", e))?;

    // Also subscribe to MARKET_SUMMARY_UPDATE as fallback for thin books
    let summary_msg = serde_json::json!({
        "type": "SUBSCRIBE",
        "subscriptions": [
            {
                "event": "MARKET_SUMMARY_UPDATE",
                "pairs": symbols
            }
        ]
    });

    let summary_json = serde_json::to_string(&summary_msg)?;
    write.send(tokio_tungstenite::tungstenite::Message::Text(summary_json.into())).await
        .map_err(|e| anyhow::anyhow!("Failed to send summary subscribe message: {}", e))?;

    println!("[INFO] Subscribed to AGGREGATED_ORDERBOOK_UPDATE and MARKET_SUMMARY_UPDATE");

    // Listen for messages
    loop {
        match read.next().await {
            Some(Ok(msg)) => {
                match msg {
                    tokio_tungstenite::tungstenite::Message::Text(text) => {
                        match serde_json::from_str::<WsResponse>(&text) {
                            Ok(response) => {
                                match response.msg_type.as_str() {
                                    "AGGREGATED_ORDERBOOK_UPDATE" => {
                                        if let (Some(symbol), Some(data)) = 
                                            (response.currency_pair_symbol, response.data) 
                                        {
                                            if let Ok(orderbook_data) = serde_json::from_value::<OrderbookData>(data) {
                                                if let (Some(bids), Some(asks)) = 
                                                    (orderbook_data.Bids, orderbook_data.Asks) 
                                                {
                                                    if let (Some(best_bid), Some(best_ask)) = (bids.first(), asks.first()) {
                                                        if let (Ok(bid), Ok(ask)) = (
                                                            best_bid.price.parse::<f64>(),
                                                            best_ask.price.parse::<f64>()
                                                        ) {
                                                            let mid = (bid + ask) / 2.0;
                                                            // Preserve existing mark_price if we already have it
                                                            let existing_mark = {
                                                                let r = prices.read().await;
                                                                r.get(&symbol).and_then(|p| p.mark_price)
                                                            };
                                                            let orderbook_prices = OrderbookPrices { bid, ask, mid, mark_price: existing_mark };
                                                            
                                                            let mut prices_write = prices.write().await;
                                                            prices_write.insert(symbol.clone(), orderbook_prices);
                                                            
                                                            // Log occasionally (every 100th update or so)
                                                            if prices_write.len() % 100 == 0 {
                                                                println!("[INFO] Updated {} for {}: bid={:.6}, ask={:.6}, mid={:.6}", 
                                                                    "orderbook", symbol, bid, ask, mid);
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                    "MARKET_SUMMARY_UPDATE" => {
                                        if let (Some(symbol), Some(data)) = 
                                            (response.currency_pair_symbol, response.data) 
                                        {
                                            if let Ok(summary_data) = serde_json::from_value::<MarketSummaryData>(data) {
                                                let mark = summary_data.mark_price
                                                    .as_deref()
                                                    .and_then(|s| s.parse::<f64>().ok());
                                                let last = summary_data.last_traded_price
                                                    .as_deref()
                                                    .and_then(|s| s.parse::<f64>().ok());

                                                let mut prices_write = prices.write().await;
                                                if let Some(existing) = prices_write.get_mut(&symbol) {
                                                    // Already have orderbook — just update mark_price
                                                    if mark.is_some() {
                                                        existing.mark_price = mark;
                                                    }
                                                } else if let Some(p) = mark.or(last) {
                                                    // No orderbook yet — bootstrap with summary price
                                                    prices_write.insert(symbol, OrderbookPrices {
                                                        bid: p, ask: p, mid: p, mark_price: mark,
                                                    });
                                                }
                                            }
                                        }
                                    }
                                    _ => {
                                        // Ignore other message types
                                    }
                                }
                            }
                            Err(e) => {
                                // Silently ignore parse errors for unknown message types
                                if cfg!(debug_assertions) {
                                    eprintln!("[WARN] Failed to parse WS message: {} - {}", text, e);
                                }
                            }
                        }
                    }
                    tokio_tungstenite::tungstenite::Message::Ping(data) => {
                        // Respond to ping with pong
                        let _: Result<(), _> = write.send(tokio_tungstenite::tungstenite::Message::Pong(data)).await;
                    }
                    tokio_tungstenite::tungstenite::Message::Close(_) => {
                        println!("[INFO] Trade WS closed by server");
                        break;
                    }
                    _ => {}
                }
            }
            Some(Err(e)) => {
                eprintln!("[ERROR] Trade WS read error: {}", e);
                return Err(anyhow::anyhow!(e));
            }
            None => {
                println!("[INFO] Trade WS stream ended");
                break;
            }
        }
    }

    Ok(())
}

// Fallback REST function using public orderbook (no auth needed)
// Returns bid, ask, and mid prices
pub async fn fetch_orderbook_prices(
    symbol: &str,
    _api_base_url: &str,
) -> Result<OrderbookPrices> {
    let client = reqwest::Client::new();
    let url = format!("https://api.valr.com/v1/public/{}/orderbook", symbol);
    
    let response = client.get(&url).send().await?;
    
    if response.status().is_success() {
        let json: serde_json::Value = response.json().await?;
        
        let bids = &json["Bids"];
        let asks = &json["Asks"];
        
        if bids.is_array() && asks.is_array() {
            if let (Some(best_bid), Some(best_ask)) = (
                bids.as_array().unwrap().first(),
                asks.as_array().unwrap().first()
            ) {
                let bid = best_bid["price"].as_str()
                    .ok_or_else(|| anyhow::anyhow!("No bid price in orderbook"))?
                    .parse::<f64>()?;
                
                let ask = best_ask["price"].as_str()
                    .ok_or_else(|| anyhow::anyhow!("No ask price in orderbook"))?
                    .parse::<f64>()?;
                
                Ok(OrderbookPrices { bid, ask, mid: (bid + ask) / 2.0, mark_price: None })
            } else {
                anyhow::bail!("Empty orderbook for {}", symbol);
            }
        } else {
            anyhow::bail!("Invalid orderbook format for {}", symbol);
        }
    } else {
        anyhow::bail!("Failed to fetch price: {}", response.status())
    }
}

// Backwards compat: just return the mid
pub async fn fetch_mark_price(
    symbol: &str,
    api_base_url: &str,
) -> Result<f64> {
    Ok(fetch_orderbook_prices(symbol, api_base_url).await?.mid)
}
