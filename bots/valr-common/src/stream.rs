/// VALR WebSocket streams.
///
/// # TradeStream — real-time price via OB_L1_DIFF
///
/// The OB_L1_DIFF feed sends:
///   - `OB_L1_SNAPSHOT` on connect: full L1 state (`d.a` = asks, `d.b` = bids)
///   - `OB_L1_DIFF` for each change: same format, only changed levels
///
/// Format: compact arrays `[["price","qty"],...]` — NOT objects.
/// Best bid = first entry of `d.b`, best ask = first entry of `d.a`.
///
/// # AccountStream — real-time fill/status events
///
/// Authenticated via HTTP upgrade headers (X-VALR-API-KEY, X-VALR-SIGNATURE, X-VALR-TIMESTAMP).
/// Sign path `/ws/account` with GET method.
/// Emits `FillEvent` for Filled and Partially Filled orders.
///
/// # Reconnection
///
/// Both streams auto-reconnect on disconnect or error, with a 5s backoff.

use std::sync::{Arc, Mutex};
use tokio::sync::{mpsc, broadcast};
use anyhow::Result;
use rust_decimal::Decimal;
use rust_decimal::prelude::FromStr;
use tracing::{info, warn};
use futures_util::{SinkExt, StreamExt};
use tokio_tungstenite::{connect_async, tungstenite::Message};
use crate::client::{Credentials, WS_TRADE_URL, WS_ACCOUNT_URL};
use crate::types::FillEvent;

// ──────────────────────────────────────────────
// Shared price state
// ──────────────────────────────────────────────

/// A cheaply cloneable handle to the live mid-price from the OB_L1_DIFF feed.
///
/// `None` until the first tick is received.
/// Broadcasts price updates via channel for real-time monitoring.
#[derive(Clone)]
pub struct TradeStream {
    mid: Arc<Mutex<Option<Decimal>>>,
    tx: broadcast::Sender<Decimal>,
}

impl TradeStream {
    /// Spawn the background task and return a handle. Reconnects automatically.
    pub fn spawn(pair: impl Into<String>) -> Self {
        let pair = pair.into();
        let mid: Arc<Mutex<Option<Decimal>>> = Arc::new(Mutex::new(None));
        let mid_clone = mid.clone();
        let (tx, _rx) = broadcast::channel::<Decimal>(1024); // Keep last 1024 prices
        let tx_clone = tx.clone();
        
        tokio::spawn(async move {
            loop {
                match run_ob_l1_diff(&pair, mid_clone.clone(), tx_clone.clone()).await {
                    Ok(_) => warn!("Trade WS disconnected ({}), reconnecting...", pair),
                    Err(e) => warn!("Trade WS error ({}): {}. Reconnecting in 5s...", pair, e),
                }
                tokio::time::sleep(tokio::time::Duration::from_secs(5)).await;
            }
        });
        TradeStream { mid, tx }
    }

    /// Returns the latest mid-price, or None if no tick received yet.
    pub fn mid_now(&self) -> Option<Decimal> {
        *self.mid.lock().unwrap()
    }

    /// Waits up to `timeout` for the first price tick, then returns it.
    pub async fn mid_wait(&self, timeout: tokio::time::Duration) -> Option<Decimal> {
        let deadline = tokio::time::Instant::now() + timeout;
        loop {
            if let Some(p) = self.mid_now() {
                return Some(p);
            }
            if tokio::time::Instant::now() >= deadline {
                return None;
            }
            tokio::time::sleep(tokio::time::Duration::from_millis(100)).await;
        }
    }
    
    /// Returns a receiver for real-time price updates.
    pub fn mid_subscribe(&self) -> broadcast::Receiver<Decimal> {
        self.tx.subscribe()
    }
}

async fn run_ob_l1_diff(pair: &str, mid: Arc<Mutex<Option<Decimal>>>, tx: broadcast::Sender<Decimal>) -> Result<()> {
    info!("Connecting trade WS: {}", WS_TRADE_URL);
    let (ws, _) = connect_async(WS_TRADE_URL).await?;
    let (mut write, mut read) = ws.split();

    let sub = serde_json::json!({
        "type": "SUBSCRIBE",
        "subscriptions": [{ "event": "OB_L1_DIFF", "pairs": [pair] }]
    });
    write.send(Message::Text(sub.to_string())).await?;
    info!("Trade WS subscribed to OB_L1_DIFF for {}", pair);

    let mut ping_timer = tokio::time::interval(tokio::time::Duration::from_secs(30));
    ping_timer.tick().await; // consume immediate
    let mut last_mid: Option<Decimal> = None;

    loop {
        tokio::select! {
            msg = read.next() => {
                match msg {
                    Some(Ok(Message::Text(text))) => {
                        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) {
                            let t = v["type"].as_str().unwrap_or("");
                            if t == "OB_L1_SNAPSHOT" || t == "OB_L1_DIFF" {
                                let d = &v["d"];
                                let bid = first_price(&d["b"]);
                                let ask = first_price(&d["a"]);
                                if let (Some(b), Some(a)) = (bid, ask) {
                                    let new_mid = (b + a) / Decimal::new(2, 0);
                                    *mid.lock().unwrap() = Some(new_mid);
                                    
                                    // Broadcast price update if it changed
                                    if last_mid != Some(new_mid) {
                                        let _ = tx.send(new_mid); // Ignore send errors (no subscribers)
                                        last_mid = Some(new_mid);
                                    }
                                }
                            }
                        }
                    }
                    Some(Ok(Message::Ping(data))) => { write.send(Message::Pong(data)).await?; }
                    Some(Ok(Message::Close(_))) | None => anyhow::bail!("WS closed"),
                    Some(Err(e)) => anyhow::bail!("WS error: {}", e),
                    _ => {}
                }
            }
            _ = ping_timer.tick() => {
                write.send(Message::Ping(vec![])).await?;
            }
        }
    }
}

/// Extract best price from OB_L1_DIFF compact array: `[["price","qty"],...]`
fn first_price(arr: &serde_json::Value) -> Option<Decimal> {
    arr.as_array()?
        .first()?
        .as_array()?
        .first()?
        .as_str()
        .and_then(|s| Decimal::from_str(s).ok())
}

// ──────────────────────────────────────────────
// Account stream
// ──────────────────────────────────────────────

/// A handle to the authenticated account WebSocket.
///
/// Drop this to stop the background task (the internal sender will close).
pub struct AccountStream;

impl AccountStream {
    /// Spawn the account WS background task.
    /// Returns a receiver for `FillEvent`s.
    ///
    /// The task auto-reconnects on disconnect. Channel capacity is 256.
    pub fn spawn(creds: Credentials) -> mpsc::Receiver<FillEvent> {
        let (tx, rx) = mpsc::channel::<FillEvent>(256);
        tokio::spawn(async move {
            loop {
                match run_account_ws(creds.clone(), tx.clone()).await {
                    Ok(_) => warn!("Account WS disconnected, reconnecting..."),
                    Err(e) => warn!("Account WS error: {}. Reconnecting in 5s...", e),
                }
                tokio::time::sleep(tokio::time::Duration::from_secs(5)).await;
                // If receiver dropped, stop retrying
                if tx.is_closed() {
                    break;
                }
            }
        });
        rx
    }
}

async fn run_account_ws(creds: Credentials, fill_tx: mpsc::Sender<FillEvent>) -> Result<()> {
    use tokio_tungstenite::tungstenite::handshake::client::Request;

    info!("Connecting account WS: {}", WS_ACCOUNT_URL);
    let (api_key, sig, ts) = creds.ws_auth_headers("/ws/account");

    let request = Request::builder()
        .uri(WS_ACCOUNT_URL)
        .header("X-VALR-API-KEY", &api_key)
        .header("X-VALR-SIGNATURE", &sig)
        .header("X-VALR-TIMESTAMP", &ts)
        .header("Host", "api.valr.com")
        .header("Connection", "Upgrade")
        .header("Upgrade", "websocket")
        .header("Sec-WebSocket-Version", "13")
        .header("Sec-WebSocket-Key", tokio_tungstenite::tungstenite::handshake::client::generate_key())
        .body(())
        .unwrap();

    let (ws, _) = connect_async(request).await?;
    let (mut write, mut read) = ws.split();

    let sub = serde_json::json!({
        "type": "SUBSCRIBE",
        "subscriptions": [{ "event": "ORDER_STATUS_UPDATE" }]
    });
    write.send(Message::Text(sub.to_string())).await?;
    info!("Account WS subscribed to ORDER_STATUS_UPDATE");

    let mut ping_timer = tokio::time::interval(tokio::time::Duration::from_secs(30));
    ping_timer.tick().await;

    loop {
        tokio::select! {
            msg = read.next() => {
                match msg {
                    Some(Ok(Message::Text(text))) => {
                        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) {
                            if v["type"] == "ORDER_STATUS_UPDATE" {
                                if let Some(fill) = parse_fill(&v["data"]) {
                                    // Don't block if receiver is full — drop the event
                                    let _ = fill_tx.try_send(fill);
                                }
                            }
                        }
                    }
                    Some(Ok(Message::Ping(data))) => { write.send(Message::Pong(data)).await?; }
                    Some(Ok(Message::Close(_))) | None => anyhow::bail!("Account WS closed"),
                    Some(Err(e)) => anyhow::bail!("Account WS error: {}", e),
                    _ => {}
                }
            }
            _ = ping_timer.tick() => {
                write.send(Message::Ping(vec![])).await?;
            }
        }
    }
}

fn parse_fill(data: &serde_json::Value) -> Option<FillEvent> {
    let status = data["orderStatusType"].as_str()?;
    // Accept any status that indicates a fill occurred
    if status != "Filled" && status != "Partially Filled" && status != "Placed" {
        return None;
    }
    let order_id = data["orderId"].as_str()?.to_string();
    let pair = data["currencyPair"].as_str()?.to_string();
    let side = data["side"].as_str()?.to_string();
    let price = data["price"].as_str()
        .and_then(|s| Decimal::from_str(s).ok())?;
    // Use filledQuantity or quantity for the fill amount
    let filled_qty = data["filledQuantity"].as_str()
        .and_then(|s| Decimal::from_str(s).ok())
        .or_else(|| data["quantity"].as_str().and_then(|s| Decimal::from_str(s).ok()))
        .unwrap_or(Decimal::ZERO);

    Some(FillEvent { order_id, pair, side, status: status.to_string(), filled_qty, price })
}
