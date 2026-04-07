/// VALR REST client with HMAC-SHA512 auth.
///
/// Auth endpoints hit the 2,000 req/min per-key limit (not the 30/min public limit).
/// Public endpoints (/v1/public/*) are limited to 30 req/min — avoid in production.

use anyhow::{Context, Result};
use chrono::Utc;
use hmac::{Hmac, Mac};
use reqwest::{Client, Method};
use rust_decimal::Decimal;
use serde_json::Value;
use sha2::Sha512;
use crate::types::Balance;

pub const BASE_URL: &str = "https://api.valr.com";
pub const WS_TRADE_URL: &str = "wss://api.valr.com/ws/trade";
pub const WS_ACCOUNT_URL: &str = "wss://api.valr.com/ws/account";

// ──────────────────────────────────────────────
// Credentials
// ──────────────────────────────────────────────

#[derive(Clone)]
pub struct Credentials {
    pub api_key: String,
    pub api_secret: String,
}

impl Credentials {
    /// Load from the encrypted vault via secrets.py
    /// Uses Grid Bot 1 credentials
    pub fn load() -> Result<Self> {
        let key = std::process::Command::new("python3")
            .args(["/home/admin/.openclaw/secrets/secrets.py", "get", "valr_api_key"])
            .output()
            .context("Failed to read valr_api_key")?;
        let secret = std::process::Command::new("python3")
            .args(["/home/admin/.openclaw/secrets/secrets.py", "get", "valr_api_secret"])
            .output()
            .context("Failed to read valr_api_secret")?;

        Ok(Self {
            api_key: String::from_utf8(key.stdout)?.trim().to_string(),
            api_secret: String::from_utf8(secret.stdout)?.trim().to_string(),
        })
    }

    /// HMAC-SHA512 signature for a request.
    pub fn sign(&self, timestamp_ms: i64, method: &str, path: &str, body: &str, subaccount_id: &str) -> String {
        let payload = format!("{}{}{}{}{}", timestamp_ms, method.to_uppercase(), path, body, subaccount_id);
        let mut mac = Hmac::<Sha512>::new_from_slice(self.api_secret.as_bytes())
            .expect("HMAC accepts any key size");
        mac.update(payload.as_bytes());
        hex::encode(mac.finalize().into_bytes())
    }

    /// Returns (api_key, signature, timestamp_ms_string) for authenticating a WebSocket upgrade.
    /// Sign against the WS path e.g. "/ws/account".
    pub fn ws_auth_headers(&self, path: &str) -> (String, String, String) {
        let ts = Utc::now().timestamp_millis();
        let sig = self.sign(ts, "GET", path, "", "");
        (self.api_key.clone(), sig, ts.to_string())
    }
}

// ──────────────────────────────────────────────
// REST client
// ──────────────────────────────────────────────

pub struct ValrClient {
    pub creds: Credentials,
    http: Client,
    /// Optional subaccount ID. When set, included in HMAC signature and request header.
    pub subaccount_id: String,
}

impl ValrClient {
    pub fn new(creds: Credentials) -> Self {
        Self {
            creds,
            http: Client::builder()
                .tcp_nodelay(true)
                .build()
                .expect("Failed to build HTTP client"),
            subaccount_id: String::new(),
        }
    }

    pub fn with_subaccount(mut self, id: &str) -> Self {
        self.subaccount_id = id.to_string();
        self
    }

    /// Authenticated GET.
    pub async fn get(&self, path: &str) -> Result<Value> {
        let ts = Utc::now().timestamp_millis();
        let sig = self.creds.sign(ts, "GET", path, "", &self.subaccount_id);
        let mut req = self.http
            .get(format!("{BASE_URL}{path}"))
            .header("X-VALR-API-KEY", &self.creds.api_key)
            .header("X-VALR-SIGNATURE", sig)
            .header("X-VALR-TIMESTAMP", ts.to_string())
            .header("Content-Type", "application/json");
        if !self.subaccount_id.is_empty() {
            req = req.header("X-VALR-SUB-ACCOUNT-ID", &self.subaccount_id);
        }
        Ok(req.send().await?.json::<Value>().await?)
    }

    /// Authenticated POST.
    pub async fn post(&self, path: &str, body: &Value) -> Result<Value> {
        let body_str = serde_json::to_string(body)?;
        let ts = Utc::now().timestamp_millis();
        let sig = self.creds.sign(ts, "POST", path, &body_str, &self.subaccount_id);
        let mut req = self.http
            .post(format!("{BASE_URL}{path}"))
            .header("X-VALR-API-KEY", &self.creds.api_key)
            .header("X-VALR-SIGNATURE", sig)
            .header("X-VALR-TIMESTAMP", ts.to_string())
            .header("Content-Type", "application/json")
            .body(body_str);
        if !self.subaccount_id.is_empty() {
            req = req.header("X-VALR-SUB-ACCOUNT-ID", &self.subaccount_id);
        }
        Ok(req.send().await?.json::<Value>().await?)
    }

    /// Authenticated DELETE.
    pub async fn delete(&self, path: &str, body: &Value) -> Result<Value> {
        let body_str = serde_json::to_string(body)?;
        let ts = Utc::now().timestamp_millis();
        let sig = self.creds.sign(ts, "DELETE", path, &body_str, &self.subaccount_id);
        let mut req = self.http
            .request(Method::DELETE, format!("{BASE_URL}{path}"))
            .header("X-VALR-API-KEY", &self.creds.api_key)
            .header("X-VALR-SIGNATURE", sig)
            .header("X-VALR-TIMESTAMP", ts.to_string())
            .header("Content-Type", "application/json")
            .body(body_str);
        if !self.subaccount_id.is_empty() {
            req = req.header("X-VALR-SUB-ACCOUNT-ID", &self.subaccount_id);
        }
        let resp = req.send().await?;
        // DELETE may return empty body (200/202) — handle gracefully
        let text = resp.text().await?;
        if text.is_empty() {
            return Ok(serde_json::json!({}));
        }
        Ok(serde_json::from_str(&text)?)
    }

    /// Public unauthenticated GET. ⚠️  30 req/min limit — avoid in hot paths.
    pub async fn get_public(&self, path: &str) -> Result<Value> {
        let resp = self.http
            .get(format!("{BASE_URL}{path}"))
            .header("Content-Type", "application/json")
            .send().await?;
        let status = resp.status();
        let text = resp.text().await?;
        if status.as_u16() == 429 {
            anyhow::bail!("429 Too Many Requests (public limit 30/min) for {}", path);
        }
        if !status.is_success() || text.is_empty() {
            anyhow::bail!("HTTP {} for {}: {}", status, path, &text[..text.len().min(200)]);
        }
        Ok(serde_json::from_str(&text)?)
    }

    // ── Convenience helpers ──────────────────

    pub async fn get_balances(&self) -> Result<Vec<Balance>> {
        let v = self.get("/v1/account/balances").await?;
        Ok(serde_json::from_value(v)?)
    }

    pub async fn get_usdt_balance(&self) -> Result<Decimal> {
        let balances = self.get_balances().await?;
        Ok(balances.iter()
            .find(|b| b.currency == "USDT")
            .and_then(|b| b.available.parse().ok())
            .unwrap_or(Decimal::ZERO))
    }

    pub async fn get_open_orders(&self) -> Result<Vec<Value>> {
        let v = self.get("/v1/orders/open").await?;
        Ok(serde_json::from_value(v).unwrap_or_default())
    }

    /// Place a limit order. Returns the order ID.
    /// post_only: if true, order will be rejected if it would match immediately
    /// time_in_force: "GTC", "IOC", or "FOK"
    pub async fn place_limit_order(
        &self,
        pair: &str,
        side: &str,
        price: &str,
        qty: &str,
        post_only: bool,
        time_in_force: &str,
    ) -> Result<String> {
        let body = serde_json::json!({
            "side": side.to_uppercase(),
            "quantity": qty,
            "price": price,
            "pair": pair,
            "postOnly": post_only,
            "timeInForce": time_in_force
        });
        let resp = self.post("/v2/orders/limit", &body).await?;
        resp.get("id")
            .and_then(|v| v.as_str())
            .map(String::from)
            .context(format!("No order ID in response: {resp}"))
    }

    /// Convenience wrapper for GTC post-only orders (backward compatible)
    pub async fn place_limit_order_gtc(
        &self,
        pair: &str,
        side: &str,
        price: &str,
        qty: &str,
        post_only: bool,
    ) -> Result<String> {
        self.place_limit_order(pair, side, price, qty, post_only, "GTC").await
    }

    /// Cancel an order by ID.
    pub async fn cancel_order(&self, pair: &str, order_id: &str) -> Result<()> {
        let body = serde_json::json!({
            "orderId": order_id,
            "pair": pair
        });
        self.delete("/v1/orders/order", &body).await?;
        Ok(())
    }

    /// Cancel a conditional order by ID.
    pub async fn cancel_conditional(&self, order_id: &str) -> Result<()> {
        let path = format!("/v1/orders/conditionals/conditional/{}", order_id);
        let body = serde_json::json!({});
        self.delete(&path, &body).await?;
        Ok(())
    }
}
