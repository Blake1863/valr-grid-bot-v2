/// Account WebSocket client for perp bot.
/// Handles order placement via PLACE_LIMIT_ORDER and balance updates.
///
/// Order placement is *two-stage*:
///   1. We send PLACE_LIMIT_ORDER with a `clientMsgId`.
///   2. VALR replies with `PLACE_LIMIT_WS_RESPONSE` (our ACK → we learn `orderId`).
///   3. VALR then emits `ORDER_STATUS_UPDATE` messages for that `orderId`. We
///      resolve the `place_order` future from the *status update*, not from the
///      ACK. This guarantees:
///        * Post-only maker: reply is Ok only if the order actually rested
///          (Placed/PartiallyFilled/Filled) rather than being rejected with
///          "Post only cancelled as it would have matched".
///        * IOC taker: reply is Ok only if the order actually matched (Filled
///          or PartiallyFilled) rather than dying on an empty price level.
///
/// `PlaceMode` drives the classification.
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

#[allow(unused_imports)]
use crate::valr::{MarginState, SharedMarginState};

type HmacSha512 = Hmac<Sha512>;

#[derive(Clone, Copy, Debug)]
pub enum PlaceMode {
    /// Post-only maker: must *rest* on book. Any Failed status = error.
    /// Filled/PartiallyFilled/Placed = Ok (it's on the book or already matched).
    Maker,
    /// IOC taker: must *match*. Filled/PartiallyFilled = Ok. Any Failed or
    /// Cancelled status = error.
    Taker,
}

struct PendingEntry {
    mode: PlaceMode,
    reply: oneshot::Sender<Result<String>>,
}

type PendingByClientMsg = Arc<RwLock<HashMap<String, PendingEntry>>>;
type PendingByOrderId = Arc<RwLock<HashMap<String, PendingEntry>>>;

pub enum WsCommand {
    PlaceOrder {
        client_msg_id: String,
        payload: serde_json::Value,
        mode: PlaceMode,
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

        let actor = WsActor {
            api_key,
            api_secret,
            subaccount_id,
            account_name: account_name.clone(),
            margin_state,
            pending_by_msg: Arc::new(RwLock::new(HashMap::new())),
            pending_by_order: Arc::new(RwLock::new(HashMap::new())),
        };

        tokio::spawn(actor.run(cmd_rx));

        WsClient { account_name, cmd_tx }
    }

    /// Place a maker (post-only GTC) order. Resolves only once the order is
    /// confirmed resting on book (or matched) — never on bare ACK.
    pub async fn place_maker(
        &self,
        pair: &str,
        side: &str,
        quantity: f64,
        price: f64,
    ) -> Result<String> {
        self.place(pair, side, quantity, price, true, "GTC", PlaceMode::Maker).await
    }

    /// Place a taker (IOC, not post-only) order. Resolves with Ok only on
    /// Filled/PartiallyFilled; error on Failed/Cancelled.
    pub async fn place_taker(
        &self,
        pair: &str,
        side: &str,
        quantity: f64,
        price: f64,
    ) -> Result<String> {
        self.place(pair, side, quantity, price, false, "IOC", PlaceMode::Taker).await
    }

    /// Legacy API — defaults to Taker mode if post_only=false, else Maker.
    pub async fn place_order(
        &self,
        pair: &str,
        side: &str,
        quantity: f64,
        price: f64,
        post_only: bool,
        time_in_force: &str,
    ) -> Result<String> {
        let mode = if post_only { PlaceMode::Maker } else { PlaceMode::Taker };
        self.place(pair, side, quantity, price, post_only, time_in_force, mode).await
    }

    async fn place(
        &self,
        pair: &str,
        side: &str,
        quantity: f64,
        price: f64,
        post_only: bool,
        time_in_force: &str,
        mode: PlaceMode,
    ) -> Result<String> {
        let client_msg_id = uuid::Uuid::new_v4().to_string();
        let customer_order_id = uuid::Uuid::new_v4().to_string();

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
            .send(WsCommand::PlaceOrder {
                client_msg_id,
                payload,
                mode,
                reply: reply_tx,
            })
            .await
            .context("WS actor channel closed")?;

        // Timeout: maker can take a bit to hit book; taker IOC is immediate
        // but VALR may take a moment to emit the terminal ORDER_STATUS_UPDATE.
        // 10s is generous; if VALR still doesn't respond, something is wrong.
        tokio::time::timeout(tokio::time::Duration::from_secs(10), reply_rx)
            .await
            .context("WS order placement timed out after 10s")?
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
    pending_by_msg: PendingByClientMsg,
    pending_by_order: PendingByOrderId,
}

impl WsActor {
    async fn run(self, mut cmd_rx: mpsc::Receiver<WsCommand>) {
        let mut backoff_secs = 30u64;
        let max_backoff = 300u64;

        loop {
            match self.connect_and_run(&mut cmd_rx).await {
                Ok(_) => {
                    println!("[INFO] [{}] WS disconnected cleanly, reconnecting in 10s...", self.account_name);
                    tokio::time::sleep(tokio::time::Duration::from_secs(10)).await;
                    backoff_secs = 30;
                }
                Err(e) => {
                    eprintln!("[ERROR] [{}] WS error: {} — reconnecting in {}s...", self.account_name, e, backoff_secs);
                    self.drain_pending(anyhow::anyhow!("WS error, reconnecting")).await;
                    tokio::time::sleep(tokio::time::Duration::from_secs(backoff_secs)).await;
                    backoff_secs = (backoff_secs * 2).min(max_backoff);
                }
            }
        }
    }

    async fn drain_pending(&self, _err: anyhow::Error) {
        {
            let mut p = self.pending_by_msg.write().await;
            for (_, e) in p.drain() {
                let _ = e.reply.send(Err(anyhow::anyhow!("WS error, reconnecting")));
            }
        }
        {
            let mut p = self.pending_by_order.write().await;
            for (_, e) in p.drain() {
                let _ = e.reply.send(Err(anyhow::anyhow!("WS error, reconnecting")));
            }
        }
    }

    async fn connect_and_run(&self, cmd_rx: &mut mpsc::Receiver<WsCommand>) -> Result<()> {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH).unwrap()
            .as_millis() as u64;

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

        // Subscribe to ORDER_STATUS_UPDATE (for order lifecycle) AND
        // NEW_ACCOUNT_TRADE (to observe our fills / detect external fills).
        // OPEN_ORDERS_UPDATE is auto-pushed but harmless to also request.
        let sub_msg = serde_json::json!({
            "type": "SUBSCRIBE",
            "subscriptions": [
                {"event": "ORDER_STATUS_UPDATE"},
                {"event": "NEW_ACCOUNT_TRADE"},
                {"event": "OPEN_ORDERS_UPDATE"}
            ]
        });
        let sub_json = serde_json::to_string(&sub_msg).unwrap_or_default();
        let _ = write.send(tokio_tungstenite::tungstenite::Message::Text(sub_json.into())).await;
        println!("[INFO] [{}] WS subscribed to ORDER_STATUS_UPDATE, NEW_ACCOUNT_TRADE, OPEN_ORDERS_UPDATE", self.account_name);
        ping_interval.tick().await;

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
                        Some(WsCommand::PlaceOrder { client_msg_id, payload, mode, reply }) => {
                            self.pending_by_msg.write().await.insert(
                                client_msg_id.clone(),
                                PendingEntry { mode, reply },
                            );
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
        // Debug: log key message types
        if let Ok(val) = serde_json::from_str::<serde_json::Value>(text) {
            let msg_type = val.get("type").and_then(|v| v.as_str()).unwrap_or("UNKNOWN");
            if msg_type == "ORDER_STATUS_UPDATE" || msg_type == "NEW_ACCOUNT_TRADE" {
                // Keep logs compact — only show status summary, not full JSON
                // (Full JSON is still emitted below on FILL/ERROR paths.)
            }
        }

        let msg: WsMessage = match serde_json::from_str(text) {
            Ok(m) => m,
            Err(_) => return,
        };

        match msg.msg_type.as_str() {
            "AUTHENTICATED" => {
                println!("[INFO] [{}] WS authenticated", self.account_name);
            }

            // Stage 1: VALR ACKs our PLACE_LIMIT_ORDER. We learn `orderId`.
            // We do NOT resolve the reply yet — we wait for an ORDER_STATUS_UPDATE.
            "PLACE_LIMIT_WS_RESPONSE" => {
                let order_id = msg.data.as_ref()
                    .and_then(|d| d.get("orderId"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("").to_string();

                if order_id.is_empty() {
                    // Malformed ACK — if we can find the pending entry by clientMsgId, fail it.
                    if let Some(cid) = &msg.client_msg_id {
                        if let Some(entry) = self.pending_by_msg.write().await.remove(cid) {
                            let _ = entry.reply.send(Err(anyhow::anyhow!(
                                "PLACE_LIMIT_WS_RESPONSE had no orderId: {:?}", msg.data
                            )));
                        }
                    }
                    return;
                }

                // Move entry from msg-keyed map to orderId-keyed map.
                if let Some(cid) = &msg.client_msg_id {
                    if let Some(entry) = self.pending_by_msg.write().await.remove(cid) {
                        self.pending_by_order.write().await.insert(order_id.clone(), entry);
                    }
                }

                println!("[INFO] [{}] WS order ACK: {}",
                    self.account_name, &order_id[..8.min(order_id.len())]);
            }

            // Stage 2: terminal status for our order — resolve the pending reply.
            "ORDER_STATUS_UPDATE" => {
                let Some(data) = msg.data.as_ref() else { return; };
                let order_id = data.get("orderId")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                if order_id.is_empty() { return; }

                let status = data.get("orderStatusType")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let failed_reason = data.get("failedReason")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");

                // Find pending entry
                let entry_opt = self.pending_by_order.write().await.remove(&order_id);
                let Some(entry) = entry_opt else { return; };

                let result: Result<String> = classify_status(entry.mode, status, failed_reason, &order_id);
                let _ = entry.reply.send(result);
            }

            "ORDER_FAILED" | "ORDER_REJECTED" => {
                let reason = msg.data.as_ref()
                    .and_then(|d| d.get("message"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown").to_string();

                eprintln!("[ERROR] [{}] WS order {}: {}", self.account_name, msg.msg_type, reason);

                // Resolve by clientMsgId if still pre-ACK
                if let Some(cid) = &msg.client_msg_id {
                    if let Some(entry) = self.pending_by_msg.write().await.remove(cid) {
                        let _ = entry.reply.send(Err(anyhow::anyhow!("{}: {}", msg.msg_type, reason)));
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

// ── Status classification ─────────────────────────────────────────────────────

/// Given a status update for our order, decide if the placement succeeded
/// or failed based on the mode (Maker vs Taker).
///
/// VALR status types observed:
///   Filled, PartiallyFilled (either matches), Partially Filled (with space),
///   Failed, Cancelled, Expired,
///   "Order Modified", "Order price has been modified due to slippage limits"
///
/// For a post-only GTC maker:
///   - Filled / PartiallyFilled => good (our taker immediately matched us)
///   - Failed with failedReason  => bad (post-only crossed, reject, etc.)
///   - Cancelled / Expired       => bad (can't match against it)
///   - Anything else (e.g. a Modified re-pricing event) => ignore and wait more
///     -- but since we remove the entry on first match, we treat 'unclassified'
///     as "still pending"? We chose to RESOLVE on every update we see for the
///     order. To avoid premature resolution on 'Modified', we re-insert the
///     entry back into the pending map for those cases.
///
/// For an IOC taker:
///   - Filled / PartiallyFilled  => good
///   - Failed / Cancelled / Expired => bad
fn classify_status(
    mode: PlaceMode,
    status: &str,
    failed_reason: &str,
    order_id: &str,
) -> Result<String> {
    // Normalise status (VALR uses both "Partially Filled" and "PartiallyFilled")
    let status_norm = status.replace(' ', "");
    let oid = order_id.to_string();

    match (mode, status_norm.as_str()) {
        (_, "Filled") | (_, "PartiallyFilled") => Ok(oid),
        (_, "Failed") => Err(anyhow::anyhow!(
            "ORDER_STATUS_UPDATE Failed: {}",
            if failed_reason.is_empty() { "unknown" } else { failed_reason }
        )),
        (_, "Cancelled") | (_, "Expired") => Err(anyhow::anyhow!(
            "ORDER_STATUS_UPDATE {}",
            if status.is_empty() { "Cancelled" } else { status }
        )),
        // For Maker: an Active/Placed status (if VALR ever emits one) means resting — success
        (PlaceMode::Maker, "Active") | (PlaceMode::Maker, "Placed")
        | (PlaceMode::Maker, "Open") | (PlaceMode::Maker, "New") => Ok(oid),
        // Unknown status — treat conservatively as failure to avoid wedging the caller
        _ => Err(anyhow::anyhow!(
            "ORDER_STATUS_UPDATE unclassified status={} reason={}", status, failed_reason
        )),
    }
}
