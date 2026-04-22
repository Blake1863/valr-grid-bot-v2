use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::RwLock;
use anyhow::Result;
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;

pub struct PriceFeed {
    prices: Arc<RwLock<HashMap<String, OrderbookPrices>>>,
    ws_url: String,
}

#[derive(Debug, Clone)]
pub struct OrderbookPrices {
    pub bid: f64,
    pub ask: f64,
    pub mid: f64,
    pub mark_price: Option<f64>,
    /// Local monotonic clock timestamp of the last update.
    /// Used to detect stale books (e.g. WS delay, gap between ticks).
    pub updated_at: Instant,
    /// Local monotonic timestamp of the previous bid/ask change.
    /// Used to detect mid-flight "jitter" — i.e. the book just moved.
    pub prev_updated_at: Option<Instant>,
}

#[derive(Debug, Deserialize)]
struct WsResponse {
    #[serde(rename = "type")]
    msg_type: String,
    #[serde(default, rename = "currencyPairSymbol")]
    currency_pair_symbol: Option<String>,
    #[serde(default)]
    data: Option<serde_json::Value>,
    // `d` key is used by OB_L1_DIFF/SNAPSHOT events
    #[serde(default)]
    d: Option<serde_json::Value>,
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

    /// Subscribe to real-time L1 (top of book) updates for each pair via OB_L1_DIFF.
    ///
    /// Each pair needs its own WS connection because OB_L1_DIFF subscriptions are
    /// per-pair. We also open ONE extra connection for MARKET_SUMMARY_UPDATE on all
    /// pairs to track mark price as a fallback.
    pub async fn connect_and_subscribe(&self, symbols: &[&str]) -> Result<()> {
        let ws_url = self.ws_url.clone();
        let prices = self.prices.clone();
        let symbols_owned: Vec<String> = symbols.iter().map(|s| s.to_string()).collect();

        // One OB_L1_DIFF connection per pair
        for sym in &symbols_owned {
            let pair = sym.clone();
            let ws_url = ws_url.clone();
            let prices = prices.clone();
            tokio::spawn(async move {
                loop {
                    match run_ob_l1_diff_loop(&ws_url, &pair, prices.clone()).await {
                        Ok(_) => println!("[WARN] OB_L1_DIFF for {} closed, reconnecting in 3s...", pair),
                        Err(e) => eprintln!("[ERROR] OB_L1_DIFF for {}: {} - reconnecting in 3s...", pair, e),
                    }
                    tokio::time::sleep(tokio::time::Duration::from_secs(3)).await;
                }
            });
        }

        // One MARKET_SUMMARY_UPDATE connection for mark prices across all pairs
        let ws_url2 = ws_url.clone();
        let prices2 = prices.clone();
        let symbols2 = symbols_owned.clone();
        tokio::spawn(async move {
            loop {
                match run_market_summary_loop(&ws_url2, &symbols2, prices2.clone()).await {
                    Ok(_) => println!("[WARN] MARKET_SUMMARY_UPDATE closed, reconnecting in 3s..."),
                    Err(e) => eprintln!("[ERROR] MARKET_SUMMARY_UPDATE: {} - reconnecting in 3s...", e),
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

    pub async fn get_price(&self, symbol: &str) -> Option<f64> {
        self.get_orderbook(symbol).await.map(|p| p.mid)
    }
}

/// Real-time L1 feed. Each message (`OB_L1_SNAPSHOT` or `OB_L1_DIFF`) carries
/// the full top-of-book in compact array form under key `d`:
///   `{ "a": [["price","qty"], ...], "b": [["price","qty"], ...] }`
async fn run_ob_l1_diff_loop(
    ws_url: &str,
    pair: &str,
    prices: Arc<RwLock<HashMap<String, OrderbookPrices>>>,
) -> Result<()> {
    let (ws_stream, _resp) = tokio_tungstenite::connect_async(ws_url)
        .await
        .map_err(|e| anyhow::anyhow!("Failed to connect WS for {}: {}", pair, e))?;
    let (mut write, mut read) = ws_stream.split::<tokio_tungstenite::tungstenite::Message>();

    let sub = serde_json::json!({
        "type": "SUBSCRIBE",
        "subscriptions": [{ "event": "OB_L1_DIFF", "pairs": [pair] }]
    });
    write
        .send(tokio_tungstenite::tungstenite::Message::Text(sub.to_string().into()))
        .await
        .map_err(|e| anyhow::anyhow!("subscribe send failed: {}", e))?;

    println!("[INFO] OB_L1_DIFF subscribed for {}", pair);

    let mut ping = tokio::time::interval(tokio::time::Duration::from_secs(20));
    ping.tick().await; // consume immediate

    loop {
        tokio::select! {
            msg = read.next() => match msg {
                Some(Ok(tokio_tungstenite::tungstenite::Message::Text(text))) => {
                    if let Ok(resp) = serde_json::from_str::<WsResponse>(&text) {
                        if resp.msg_type == "OB_L1_SNAPSHOT" || resp.msg_type == "OB_L1_DIFF" {
                            if let Some(d) = resp.d.as_ref() {
                                let bid = first_price(&d["b"]);
                                let ask = first_price(&d["a"]);
                                if let (Some(b), Some(a)) = (bid, ask) {
                                    let mid = (b + a) / 2.0;
                                    let now = Instant::now();
                                    let mut w = prices.write().await;
                                    let (existing_mark, prev_updated) = w.get(pair)
                                        .map(|p| (p.mark_price, Some(p.updated_at)))
                                        .unwrap_or((None, None));
                                    w.insert(pair.to_string(), OrderbookPrices {
                                        bid: b,
                                        ask: a,
                                        mid,
                                        mark_price: existing_mark,
                                        updated_at: now,
                                        prev_updated_at: prev_updated,
                                    });
                                }
                            }
                        }
                    }
                }
                Some(Ok(tokio_tungstenite::tungstenite::Message::Ping(d))) => {
                    let _ = write.send(tokio_tungstenite::tungstenite::Message::Pong(d)).await;
                }
                Some(Ok(tokio_tungstenite::tungstenite::Message::Close(_))) => {
                    println!("[INFO] OB_L1_DIFF WS closed by server for {}", pair);
                    return Ok(());
                }
                Some(Err(e)) => return Err(anyhow::anyhow!("WS read error: {}", e)),
                None => return Ok(()),
                _ => {}
            },
            _ = ping.tick() => {
                if let Err(e) = write
                    .send(tokio_tungstenite::tungstenite::Message::Ping(vec![].into()))
                    .await
                {
                    return Err(anyhow::anyhow!("ping failed: {}", e));
                }
            }
        }
    }
}

/// Secondary stream: mark price for wide-spread fallback.
async fn run_market_summary_loop(
    ws_url: &str,
    symbols: &[String],
    prices: Arc<RwLock<HashMap<String, OrderbookPrices>>>,
) -> Result<()> {
    let (ws_stream, _resp) = tokio_tungstenite::connect_async(ws_url)
        .await
        .map_err(|e| anyhow::anyhow!("MARKET_SUMMARY_UPDATE connect failed: {}", e))?;
    let (mut write, mut read) = ws_stream.split::<tokio_tungstenite::tungstenite::Message>();

    let sub = serde_json::json!({
        "type": "SUBSCRIBE",
        "subscriptions": [{ "event": "MARKET_SUMMARY_UPDATE", "pairs": symbols }]
    });
    write
        .send(tokio_tungstenite::tungstenite::Message::Text(sub.to_string().into()))
        .await?;

    println!("[INFO] MARKET_SUMMARY_UPDATE subscribed for {} pairs", symbols.len());

    let mut ping = tokio::time::interval(tokio::time::Duration::from_secs(20));
    ping.tick().await;

    loop {
        tokio::select! {
            msg = read.next() => match msg {
                Some(Ok(tokio_tungstenite::tungstenite::Message::Text(text))) => {
                    if let Ok(resp) = serde_json::from_str::<WsResponse>(&text) {
                        if resp.msg_type == "MARKET_SUMMARY_UPDATE" {
                            if let (Some(symbol), Some(data)) = (resp.currency_pair_symbol, resp.data) {
                                if let Ok(sd) = serde_json::from_value::<MarketSummaryData>(data) {
                                    let mark = sd.mark_price
                                        .as_deref().and_then(|s| s.parse::<f64>().ok());
                                    let last = sd.last_traded_price
                                        .as_deref().and_then(|s| s.parse::<f64>().ok());

                                    let mut w = prices.write().await;
                                    if let Some(p) = w.get_mut(&symbol) {
                                        if mark.is_some() { p.mark_price = mark; }
                                    } else if let Some(px) = mark.or(last) {
                                        let now = Instant::now();
                                        w.insert(symbol, OrderbookPrices {
                                            bid: px, ask: px, mid: px,
                                            mark_price: mark,
                                            updated_at: now,
                                            prev_updated_at: None,
                                        });
                                    }
                                }
                            }
                        }
                    }
                }
                Some(Ok(tokio_tungstenite::tungstenite::Message::Ping(d))) => {
                    let _ = write.send(tokio_tungstenite::tungstenite::Message::Pong(d)).await;
                }
                Some(Ok(tokio_tungstenite::tungstenite::Message::Close(_))) => return Ok(()),
                Some(Err(e)) => return Err(anyhow::anyhow!("WS read error: {}", e)),
                None => return Ok(()),
                _ => {}
            },
            _ = ping.tick() => {
                if let Err(e) = write
                    .send(tokio_tungstenite::tungstenite::Message::Ping(vec![].into()))
                    .await
                {
                    return Err(anyhow::anyhow!("ping failed: {}", e));
                }
            }
        }
    }
}

fn first_price(arr: &serde_json::Value) -> Option<f64> {
    arr.as_array()?
        .first()?
        .as_array()?
        .first()?
        .as_str()
        .and_then(|s| s.parse::<f64>().ok())
}

// Fallback REST function using public orderbook (no auth needed)
pub async fn fetch_orderbook_prices(
    symbol: &str,
    _api_base_url: &str,
) -> Result<OrderbookPrices> {
    let client = reqwest::Client::new();
    let url = format!("https://api.valr.com/v1/public/{}/orderbook", symbol);
    let response = client.get(&url).send().await?;
    if !response.status().is_success() {
        anyhow::bail!("Failed to fetch price: {}", response.status());
    }
    let json: serde_json::Value = response.json().await?;
    let bids = &json["Bids"];
    let asks = &json["Asks"];
    if !(bids.is_array() && asks.is_array()) {
        anyhow::bail!("Invalid orderbook format for {}", symbol);
    }
    let best_bid = bids.as_array().unwrap().first()
        .ok_or_else(|| anyhow::anyhow!("Empty bids for {}", symbol))?;
    let best_ask = asks.as_array().unwrap().first()
        .ok_or_else(|| anyhow::anyhow!("Empty asks for {}", symbol))?;
    let bid = best_bid["price"].as_str()
        .ok_or_else(|| anyhow::anyhow!("No bid price"))?
        .parse::<f64>()?;
    let ask = best_ask["price"].as_str()
        .ok_or_else(|| anyhow::anyhow!("No ask price"))?
        .parse::<f64>()?;
    Ok(OrderbookPrices {
        bid, ask, mid: (bid + ask) / 2.0,
        mark_price: None,
        updated_at: Instant::now(),
        prev_updated_at: None,
    })
}

// Backwards compat: just return the mid
pub async fn fetch_mark_price(
    symbol: &str,
    api_base_url: &str,
) -> Result<f64> {
    Ok(fetch_orderbook_prices(symbol, api_base_url).await?.mid)
}
