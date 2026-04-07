/// Account WebSocket client for perp bot.
/// Handles order placement via PLACE_LIMIT_ORDER and balance updates.
use anyhow::{Context, Result};
use futures_util::{SinkExt, StreamExt};
use hmac::{Hmac, Mac};
use sha2::Sha512;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::sync::{mpsc, oneshot, RwLock};
use tokio_tungstenite::tungstenite::http::Request;
use tokio_tungstenite::tungstenite::handshake::client::generate_key;

use crate::valr::{MarginState, SharedMarginState};

type HmacSha512 = Hmac<Sha512>;
type PendingOrders = Arc<RwLock<HashMap<String, oneshot::Sender<Result<String>>>>>;

pub enum WsCommand {
    PlaceOrder {
        client_msg_id: String,
        payload: serde_json::Value,
        reply: oneshot::Sender<Result<String>>,
    },
}

/// Clone-safe handle to a running WS actor.
#[derive(Clone)]
pub struct WsClient {
    pub account_name: String,
    cmd_tx: mpsc::Sender<WsCommand>,
}

#[derive(Debug, Serialize)]
struct PlaceOrderMsg {
    #[serde(rename = "type")]
    msg_type: String,
    #[serde(rename = "clientMsgId")]
    client_msg_id: String,
    payload: serde_json::Value,
}

#[derive(Debug, Deserialize)]
struct WsMessage {
    #[serde(rename = "type")]
    msg_type: String,
    #[serde(default, rename = "clientMsgId")]
    client_msg_id: Option<String>,
    #[serde(default)]
    data: Option<serde_json::Value>,
}

impl WsClient {
    /// Create a WsClient and spawn the actor. Call once at startup.
    pub async fn new(
        api_key: String,
        api_secret: String,
        subaccount_id: Option<String>,
        margin_state: SharedMarginState,
        account_name: String,
    ) -> WsClient {
        let (cmd_tx, cmd_rx) = mpsc::channel::<WsCommand>(64);
        let pending: PendingOrders = Arc::new(RwLock::new(HashMap::new()));

        let actor = WsActor {
            api_key,
            api_secret,
            subaccount_id,
            account_name: account_name.clone(),
            margin_state,
            pending,
        };

        tokio::spawn(actor.run(cmd_rx));

        WsClient { account_name, cmd_tx }
    }
}

impl WsClient {
    /// Place a limit order via WS. Awaits PLACE_LIMIT_WS_RESPONSE confirmation.
    pub async fn place_order(
        &self,
        pair: &str,
        side: &str,
        quantity: f64,
        price: f64,
        post_only: bool,
        time_in_force: &str,
    ) -> Result<String> {
        let client_msg_id = uuid::Uuid::new_v4().to_string();
        let customer_order_id = uuid::Uuid::new_v4().to_string();

        // Use plain postOnly — postOnlyReprice would reprice away from our calculated
        // price, causing the taker to miss the maker and fill externally.
        let payload = serde_json::json!({
            "side": side,
            "quantity": format!("{:.8}", quantity),
            "price": format!("{}", price),
            "pair": pair,
            "postOnly": post_only,
            "timeInForce": time_in_force,
            "customerOrderId": customer_order_id
        });

        let (reply_tx, reply_rx) = oneshot::channel();
        self.cmd_tx
            .send(WsCommand::PlaceOrder { client_msg_id, payload, reply: reply_tx })
            .await
            .context("WS actor channel closed")?;

        tokio::time::timeout(tokio::time::Duration::from_secs(5), reply_rx)
            .await
            .context("WS order placement timed out after 5s")?
            .context("WS reply channel dropped")?
    }
}

// ── Actor ─────────────────────────────────────────────────────────────────────

struct WsActor {
    api_key: String,
    api_secret: String,
    subaccount_id: Option<String>,
    account_name: String,
    margin_state: SharedMarginState,
    pending: PendingOrders,
}

impl WsActor {
    async fn run(self, mut cmd_rx: mpsc::Receiver<WsCommand>) {
        // Start with 30 second backoff to avoid rate limiting
        // VALR WS can be sensitive to rapid reconnects
        let mut backoff_secs = 30u64;
        let max_backoff = 300u64; // 5 minutes max
        
        loop {
            match self.connect_and_run(&mut cmd_rx).await {
                Ok(_) => {
                    // Clean disconnect — reset backoff and reconnect after short delay
                    println!("[INFO] [{}] WS disconnected cleanly, reconnecting in 10s...", self.account_name);
                    tokio::time::sleep(tokio::time::Duration::from_secs(10)).await;
                    backoff_secs = 30;
                }
                Err(e) => {
                    eprintln!("[ERROR] [{}] WS error: {} — reconnecting in {}s...", self.account_name, e, backoff_secs);
                    {
                        let mut pending = self.pending.write().await;
                        for (_, tx) in pending.drain() {
                            let _ = tx.send(Err(anyhow::anyhow!("WS error, reconnecting")));
                        }
                    }
                    tokio::time::sleep(tokio::time::Duration::from_secs(backoff_secs)).await;
                    // Exponential backoff with max cap
                    backoff_secs = (backoff_secs * 2).min(max_backoff);
                }
            }
        }
    }

    async fn connect_and_run(&self, cmd_rx: &mut mpsc::Receiver<WsCommand>) -> Result<()> {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH).unwrap()
            .as_millis() as u64;

        // Sign with empty body and actual subaccount_id (if present)
        // VALR WebSocket auth requires subaccount_id in both header AND signature
        let subaccount_for_sig = self.subaccount_id.as_deref().unwrap_or("");
        let message = format!("{}GET/ws/account{}", timestamp, subaccount_for_sig);
        let mut mac = HmacSha512::new_from_slice(self.api_secret.as_bytes()).unwrap();
        mac.update(message.as_bytes());
        let signature = hex::encode(mac.finalize().into_bytes());

        let subaccount_str = self.subaccount_id.as_deref().unwrap_or("");
        let request = Request::builder()
            .uri("wss://api.valr.com/ws/account")
            .header("X-VALR-API-KEY", &self.api_key)
            .header("X-VALR-SIGNATURE", &signature)
            .header("X-VALR-TIMESTAMP", timestamp.to_string())
            .header("X-VALR-SUB-ACCOUNT-ID", subaccount_str)
            .header("Host", "api.valr.com")
            .header("Connection", "Upgrade")
            .header("Upgrade", "websocket")
            .header("Sec-WebSocket-Version", "13")
            .header("Sec-WebSocket-Key", generate_key())
            .body(())
            .context("Failed to build WS request")?;

        let (ws_stream, _) = tokio_tungstenite::connect_async(request)
            .await
            .context("Failed to connect to WS")?;

        println!("[INFO] [{}] WS connected", self.account_name);
        let (mut write, mut read) = ws_stream.split();
        let mut ping_interval = tokio::time::interval(tokio::time::Duration::from_secs(20));
        
        // Wait for AUTHENTICATED message (with timeout)
        // Grid bot doesn't explicitly wait - it just subscribes and listens
        println!("[INFO] [{}] Waiting for AUTHENTICATED...", self.account_name);
        match tokio::time::timeout(tokio::time::Duration::from_secs(3), read.next()).await {
            Ok(Some(Ok(tokio_tungstenite::tungstenite::Message::Text(text)))) => {
                if text.contains("\"type\":\"AUTHENTICATED\"") || text.contains("\"type\": \"AUTHENTICATED\"") {
                    println!("[INFO] [{}] ✅ AUTHENTICATED received", self.account_name);
                } else if text.contains("\"type\":\"ERROR\"") || text.contains("\"type\": \"ERROR\"") {
                    eprintln!("[ERROR] [{}] WS auth error: {}", self.account_name, text);
                    anyhow::bail!("WS authentication failed: {}", text);
                } else {
                    println!("[WARN] [{}] Unexpected first message: {}", self.account_name, text);
                }
            }
            Ok(Some(Ok(other))) => {
                println!("[WARN] [{}] Unexpected message type: {:?}", self.account_name, other);
            }
            Ok(Some(Err(e))) => {
                eprintln!("[ERROR] [{}] WS read error: {}", self.account_name, e);
                anyhow::bail!("WS read error: {}", e);
            }
            Ok(None) => {
                eprintln!("[ERROR] [{}] WS stream ended unexpectedly", self.account_name);
                anyhow::bail!("WS stream ended");
            }
            Err(_) => {
                eprintln!("[ERROR] [{}] Timeout waiting for AUTHENTICATED", self.account_name);
                anyhow::bail!("WS auth timeout");
            }
        }
        
        // Subscribe to ORDER_STATUS_UPDATE only (BALANCE_UPDATE is auto-pushed)
        // This matches the grid bot's valr-common/src/stream.rs implementation
        let sub_msg = serde_json::json!({
            "type": "SUBSCRIBE",
            "subscriptions": [
                {"event": "ORDER_STATUS_UPDATE"}
            ]
        });
        let sub_json = serde_json::to_string(&sub_msg).unwrap_or_default();
        let _ = write.send(tokio_tungstenite::tungstenite::Message::Text(sub_json.into())).await;
        println!("[INFO] [{}] WS subscribed to ORDER_STATUS_UPDATE", self.account_name);
        ping_interval.tick().await; // consume first immediate tick

        loop {
            tokio::select! {
                msg = read.next() => {
                    match msg {
                        Some(Ok(tokio_tungstenite::tungstenite::Message::Text(text))) => {
                            self.handle_message(&text).await;
                        }
                        Some(Ok(tokio_tungstenite::tungstenite::Message::Ping(d))) => {
                            let _ = write.send(tokio_tungstenite::tungstenite::Message::Pong(d)).await;
                        }
                        Some(Ok(tokio_tungstenite::tungstenite::Message::Close(_))) => {
                            println!("[INFO] [{}] WS closed by server", self.account_name);
                            return Ok(());
                        }
                        Some(Err(e)) => return Err(anyhow::anyhow!("WS read error: {}", e)),
                        None => return Ok(()),
                        _ => {}
                    }
                }

                cmd = cmd_rx.recv() => {
                    match cmd {
                        Some(WsCommand::PlaceOrder { client_msg_id, payload, reply }) => {
                            self.pending.write().await.insert(client_msg_id.clone(), reply);
                            let msg = serde_json::to_string(&PlaceOrderMsg {
                                msg_type: "PLACE_LIMIT_ORDER".to_string(),
                                client_msg_id,
                                payload,
                            })?;
                            if let Err(e) = write.send(
                                tokio_tungstenite::tungstenite::Message::Text(msg.into())
                            ).await {
                                return Err(anyhow::anyhow!("WS write error: {}", e));
                            }
                        }
                        None => return Ok(()),
                    }
                }

                _ = ping_interval.tick() => {
                    if let Err(e) = write.send(
                        tokio_tungstenite::tungstenite::Message::Ping(vec![].into())
                    ).await {
                        return Err(anyhow::anyhow!("WS ping error: {}", e));
                    }
                }
            }
        }
    }

    async fn handle_message(&self, text: &str) {
        // Debug: log all message types to understand what VALR sends
        if let Ok(val) = serde_json::from_str::<serde_json::Value>(text) {
            let msg_type = val.get("type").and_then(|v| v.as_str()).unwrap_or("UNKNOWN");
            // Log fill-related messages
            if msg_type.contains("TRADE") || msg_type.contains("FILL") || (msg_type.contains("ORDER") && val.get("data").is_some()) {
                println!("[WS-MSG] [{}] type={} data={}", self.account_name, msg_type, val.get("data").unwrap_or(&serde_json::Value::Null));
            }
        }
        
        let msg: WsMessage = match serde_json::from_str(text) {
            Ok(m) => m,
            Err(_) => return,
        };

        match msg.msg_type.as_str() {
            "AUTHENTICATED" => {
                println!("[INFO] [{}] WS authenticated", self.account_name);
                // (These are not auto-pushed, need explicit subscription)
            }

            "PLACE_LIMIT_WS_RESPONSE" => {
                let order_id = msg.data.as_ref()
                    .and_then(|d| d.get("orderId"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("").to_string();

                println!("[INFO] [{}] WS order confirmed: {}",
                    self.account_name, &order_id[..8.min(order_id.len())]);

                if let Some(cid) = &msg.client_msg_id {
                    if let Some(tx) = self.pending.write().await.remove(cid) {
                        let _ = tx.send(Ok(order_id));
                    }
                }
            }

            "ORDER_FAILED" | "ORDER_REJECTED" => {
                let reason = msg.data.as_ref()
                    .and_then(|d| d.get("message"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown").to_string();

                eprintln!("[ERROR] [{}] WS order {}: {}", self.account_name, msg.msg_type, reason);

                if let Some(cid) = &msg.client_msg_id {
                    if let Some(tx) = self.pending.write().await.remove(cid) {
                        let _ = tx.send(Err(anyhow::anyhow!("{}: {}", msg.msg_type, reason)));
                    }
                }
            }

            "BALANCE_UPDATE" => {
                if let Some(data) = &msg.data {
                    let symbol = data.get("currency")
                        .and_then(|c| c.get("symbol"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("");

                    let available = data.get("availableInReference")
                        .and_then(|v| v.as_str())
                        .and_then(|s| s.parse::<f64>().ok())
                        .unwrap_or(0.0);

                    let mut bal = self.margin_state.write().await;

                    // For perp bot, track USDT available per account
                    if symbol == "USDT" {
                        if self.account_name == "CM1" {
                            bal.cm1_available = available;
                            println!("[INFO] [{}] USDT available: {:.2}", self.account_name, available);
                        } else if self.account_name == "CM2" {
                            bal.cm2_available = available;
                            println!("[INFO] [{}] USDT available: {:.2}", self.account_name, available);
                        }
                    }
                }
            }

            "OPEN_ORDERS_UPDATE" | "OPEN_POSITION_UPDATE" => {}

            "NEW_ACCOUNT_TRADE" => {
                if let Some(data) = &msg.data {
                    let pair = data.get("currencyPair")
                        .and_then(|v| v.as_str())
                        .unwrap_or("UNKNOWN");
                    let side = data.get("side")
                        .and_then(|v| v.as_str())
                        .unwrap_or("UNKNOWN");
                    let price = data.get("price")
                        .and_then(|v| v.as_str())
                        .unwrap_or("0");
                    let qty = data.get("quantity")
                        .and_then(|v| v.as_str())
                        .unwrap_or("0");
                    let fee = data.get("fee")
                        .and_then(|v| v.as_str())
                        .unwrap_or("0");
                    let fee_currency = data.get("feeCurrency")
                        .and_then(|v| v.as_str())
                        .unwrap_or("UNKNOWN");
                    let trade_id = data.get("id")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");

                    println!("[FILL] [{}] {} {} @ {} x {} | fee: {} {} | trade: {}",
                        self.account_name, pair, side, price, qty, fee, fee_currency,
                        &trade_id[..8.min(trade_id.len())]);
                }
            }

            _ => {}
        }
    }
}
