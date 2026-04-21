/// cm-bot-spot — VALR Spot Chart Maintenance Bot
///
/// Wash trades spot pairs between CMS1 and CMS2.
/// Uses account WebSocket for order placement + balance updates.
/// Uses trade WebSocket for orderbook prices.
/// 6-cycle rotation: Phase 0 (cycles 0-2): CMS1 sells to CMS2, Phase 1 (cycles 3-5): CMS2 sells to CMS1.

mod ws_client;
mod price_feed;
mod rebalance;
mod liquidator;
mod random_maker;

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs;
use std::path::Path;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use std::process::Command;
use tokio::sync::RwLock;

use ws_client::{WsClient, new_ws_client};
use price_feed::PriceFeed;

const CONFIG_PATH: &str = "config.json";
const STATE_PATH: &str = "state.json";
const API_BASE: &str = "https://api.valr.com";
const WS_TRADE_URL: &str = "wss://api.valr.com/ws/trade";
const FAILURE_THRESHOLD: u32 = 3;

/// Load a secret from the encrypted vault via secrets.py
fn load_secret(name: &str) -> Result<String> {
    let output = Command::new("python3")
        .args(["/home/admin/.openclaw/secrets/secrets.py", "get", name])
        .output()
        .context(format!("Failed to read {}", name))?;
    Ok(String::from_utf8(output.stdout)?.trim().to_string())
}

// ── Config ────────────────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct PairConfig {
    enabled: bool,
}

#[derive(Debug, Deserialize)]
struct Config {
    pairs: HashMap<String, PairConfig>,
    cycle_interval_ms: u64,
    cleanup_interval_ms: u64,
    cleanup_age_threshold_ms: u64,
    qty_range_min_multiplier: f64,
    qty_range_max_multiplier: f64,
    #[serde(default = "default_rebalance_interval")]
    rebalance_interval_cycles: u64,
}

fn default_rebalance_interval() -> u64 {
    6
}

impl Config {
    fn load(path: &str) -> Result<Self> {
        let content = fs::read_to_string(path).context("Failed to read config.json")?;
        serde_json::from_str(&content).context("Failed to parse config.json")
    }

    fn enabled_pairs(&self) -> Vec<String> {
        self.pairs.iter()
            .filter(|(_, v)| v.enabled)
            .map(|(k, _)| k.clone())
            .collect()
    }
}

// ── Pair Info ─────────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
struct PairInfo {
    symbol: String,
    base_currency: String,
    quote_currency: String,
    price_precision: u8,
    qty_precision: u8,
    min_qty: f64,
    min_value: f64,
}

async fn fetch_pair_info(symbol: &str) -> Result<PairInfo> {
    let client = reqwest::Client::new();
    let url = format!("{}/v1/public/pairs", API_BASE);
    let pairs: Vec<serde_json::Value> = client.get(&url).send().await?.json().await?;

    let pair = pairs.iter()
        .find(|p| p["symbol"].as_str() == Some(symbol))
        .context(format!("Pair {} not found in VALR API", symbol))?;

    let tick_size = pair["tickSize"].as_str().unwrap_or("0.01");
    let price_precision = if tick_size.contains('.') {
        tick_size.trim_end_matches('0').split('.').nth(1).map(|s| s.len() as u8).unwrap_or(2)
    } else { 0 };

    let qty_precision = pair["baseDecimalPlaces"].as_str()
        .and_then(|s| s.parse().ok())
        .unwrap_or(8u8);

    let min_qty = pair["minBaseAmount"].as_str()
        .and_then(|s| s.parse().ok())
        .unwrap_or(0.04f64);

    let min_value = pair["minQuoteAmount"].as_str()
        .and_then(|s| s.parse().ok())
        .unwrap_or(10.0f64);

    let base = pair["baseCurrency"].as_str().unwrap_or("LINK").to_string();
    let quote = pair["quoteCurrency"].as_str().unwrap_or("ZAR").to_string();

    Ok(PairInfo {
        symbol: symbol.to_string(),
        base_currency: base,
        quote_currency: quote,
        price_precision,
        qty_precision,
        min_qty,
        min_value,
    })
}

// ── Balance state ────────────────────────────────────────────────────────────

#[derive(Debug, Default, Clone)]
struct SpotBalance {
    zar_available: f64,
    base_available: f64,
}

type SharedBalance = Arc<RwLock<SpotBalance>>;

// ── State ─────────────────────────────────────────────────────────────────────

#[derive(Debug, Serialize, Deserialize)]
struct PairState {
    cycle_count: u64,
    total_trades: u64,
}

impl Default for PairState {
    fn default() -> Self { Self { cycle_count: 0, total_trades: 0 } }
}

fn load_state(path: &str) -> HashMap<String, PairState> {
    if Path::new(path).exists() {
        if let Ok(content) = fs::read_to_string(path) {
            if let Ok(state) = serde_json::from_str(&content) {
                return state;
            }
        }
    }
    HashMap::new()
}

fn save_state(path: &str, state: &HashMap<String, PairState>) {
    if let Ok(content) = serde_json::to_string_pretty(state) {
        let tmp = format!("{}.tmp", path);
        if fs::write(&tmp, &content).is_ok() {
            let _ = fs::rename(&tmp, path);
        }
    }
}

// ── REST helpers ──────────────────────────────────────────────────────────────

fn timestamp_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64
}

fn sign(secret: &str, method: &str, path: &str, body: &str, subaccount: &str) -> (u64, String) {
    use hmac::{Hmac, Mac};
    use sha2::Sha512;
    let ts = timestamp_ms();
    let msg = format!("{}{}{}{}{}", ts, method.to_uppercase(), path, body, subaccount);
    let mut mac = Hmac::<Sha512>::new_from_slice(secret.as_bytes()).unwrap();
    mac.update(msg.as_bytes());
    (ts, hex::encode(mac.finalize().into_bytes()))
}

async fn cancel_order(
    client: &reqwest::Client,
    key: &str, secret: &str, subaccount: &str,
    pair: &str, order_id: &str,
) -> Result<()> {
    let path = "/v1/orders/order";
    let body = serde_json::json!({"orderId": order_id, "pair": pair});
    let body_str = serde_json::to_string(&body)?;
    let (ts, sig) = sign(secret, "DELETE", path, &body_str, subaccount);

    let resp = client.delete(format!("{}{}", API_BASE, path))
        .header("X-VALR-API-KEY", key)
        .header("X-VALR-SIGNATURE", sig)
        .header("X-VALR-TIMESTAMP", ts.to_string())
        .header("X-VALR-SUB-ACCOUNT-ID", subaccount)
        .header("Content-Type", "application/json")
        .body(body_str)
        .send().await?;

    if resp.status().is_success() || resp.status().as_u16() == 202 || resp.status().as_u16() == 200 {
        Ok(())
    } else {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        anyhow::bail!("Cancel failed ({}): {}", status, text)
    }
}

async fn cancel_all_orders_for_pair(
    client: &reqwest::Client,
    key: &str, secret: &str, subaccount: &str,
    pair: &str,
) -> Result<usize> {
    let path = "/v1/orders/open";
    let (ts, sig) = sign(secret, "GET", path, "", subaccount);

    let resp = client.get(format!("{}{}", API_BASE, path))
        .header("X-VALR-API-KEY", key)
        .header("X-VALR-SIGNATURE", sig)
        .header("X-VALR-TIMESTAMP", ts.to_string())
        .header("X-VALR-SUB-ACCOUNT-ID", subaccount)
        .send().await?;

    let orders: Vec<serde_json::Value> = resp.json().await.unwrap_or_default();
    
    let pair_orders: Vec<_> = orders.into_iter()
        .filter(|o| o["currencyPair"].as_str() == Some(pair))
        .collect();
    
    let mut cancelled = 0;
    for order in pair_orders {
        let oid = order["orderId"].as_str().unwrap_or("");
        if oid.is_empty() { continue; }
        match cancel_order(client, key, secret, subaccount, pair, oid).await {
            Ok(_) => cancelled += 1,
            Err(e) => eprintln!("[WARN] Failed to cancel order {} for {}: {}", &oid[..8.min(oid.len())], pair, e),
        }
    }
    
    Ok(cancelled)
}

async fn cancel_stale_orders(
    client: &reqwest::Client,
    key: &str, secret: &str, subaccount: &str,
    account: &str, pairs: &[String], age_threshold_ms: u64,
) {
    let path = "/v1/orders/open";
    let (ts, sig) = sign(secret, "GET", path, "", subaccount);

    let resp = client.get(format!("{}{}", API_BASE, path))
        .header("X-VALR-API-KEY", key)
        .header("X-VALR-SIGNATURE", sig)
        .header("X-VALR-TIMESTAMP", ts.to_string())
        .header("X-VALR-SUB-ACCOUNT-ID", subaccount)
        .send().await;

    let orders = match resp {
        Ok(r) => r.json::<Vec<serde_json::Value>>().await.unwrap_or_default(),
        Err(_) => return,
    };

    let now_ms = timestamp_ms();
    for order in orders {
        let pair = order["currencyPair"].as_str().unwrap_or("");
        if !pairs.contains(&pair.to_string()) { continue; }

        let created_str = order["createdAt"].as_str().unwrap_or("");
        if let Ok(created) = chrono::DateTime::parse_from_rfc3339(created_str) {
            let age_ms = now_ms.saturating_sub(created.timestamp_millis() as u64);
            if age_ms > age_threshold_ms {
                let oid = order["orderId"].as_str().unwrap_or("");
                let oid_short = &oid[..8.min(oid.len())];
                match cancel_order(client, key, secret, subaccount, pair, oid).await {
                    Ok(_) => println!("[INFO] Cleanup: cancelled stale {} order {} for {} (age {}ms)",
                        account, oid_short, pair, age_ms),
                    Err(e) => eprintln!("[WARN] Cleanup cancel failed for {} {}: {}", account, oid_short, e),
                }
            }
        }
    }
}

// ── Cycle execution ───────────────────────────────────────────────────────────

fn round_price(price: f64, precision: u8) -> f64 {
    let factor = 10f64.powi(precision as i32);
    (price * factor).round() / factor
}

fn round_qty_up(qty: f64, precision: u8) -> f64 {
    let factor = 10f64.powi(precision as i32);
    (qty * factor).ceil() / factor
}

/// Execute one wash trade cycle.
/// 
/// WASH TRADE LOGIC (from first principles):
/// - We control BOTH sides: CMS1 and CMS2
/// - Goal: CMS1 and CMS2 trade WITH EACH OTHER, not with external liquidity
/// - Strategy: 
///   1. Maker places RESTING order (postOnly) at mid price
///   2. Taker places AGGRESSIVE order that crosses the spread to hit maker
///   3. Taker must be priced to hit MAKER specifically, not external orders
/// 
/// For CMS1 SELL / CMS2 BUY:
/// - CMS1 (maker): SELL limit at MID (rests on ask side of book)
/// - CMS2 (taker): BUY limit at MID or slightly above (crosses spread, hits CMS1's sell)
/// 
/// For CMS2 SELL / CMS1 BUY:
/// - CMS2 (maker): BUY limit at MID (rests on bid side of book)  
/// - CMS1 (taker): SELL limit at MID or slightly below (crosses spread, hits CMS2's buy)
/// 
/// Key insight: Both orders at MID price ensures they match with each other,
/// not external liquidity. The maker waits on book, taker crosses to hit it.
async fn execute_cycle(
    http: &reqwest::Client,
    key: &str, secret: &str,
    cms1_id: &str, cms2_id: &str,
    cms1_ws: &WsClient,
    cms2_ws: &WsClient,
    pair_info: &PairInfo,
    prices: &price_feed::OrderbookPrices,
    _cms1_balance: &SharedBalance,
    _cms2_balance: &SharedBalance,
    maker_account: &str,
    maker_side: &str,
    qty_min_mult: f64,
    qty_max_mult: f64,
    liquidator: &liquidator::Liquidator,
) -> bool {
    use rand::Rng;
    let mut rng = rand::thread_rng();

    // Calculate tick size for this pair
    let tick_size = 1.0 / (10.0_f64.powi(pair_info.price_precision as i32));
    
    // WASH TRADE PRICING - Simple Internal Matching:
    // 
    // Goal: CMS1 and CMS2 trade WITH EACH OTHER at the same price.
    // 
    // Strategy:
    // 1. Both orders placed at MID price
    // 2. CMS1 places SELL, CMS2 places BUY (or vice versa)
    // 3. Since both are at same price, they will match internally
    // 4. Use regular limit orders (not postOnly, not IOC)
    // 5. Cancel any remaining orders after sufficient time
    
    let mid_price = prices.mid;
    if mid_price <= 0.0 {
        eprintln!("[ERROR] Invalid mid price for {}", pair_info.symbol);
        return false;
    }

    // Both orders at MID price ensures internal matching
    let maker_price = round_price(mid_price, pair_info.price_precision);
    let taker_price = round_price(mid_price, pair_info.price_precision);

    // Quantity calculation
    let multiplier = rng.gen_range(qty_min_mult..=qty_max_mult);
    let min_value_qty = pair_info.min_value / taker_price;
    let effective_min = pair_info.min_qty.max(min_value_qty);
    let raw_qty = round_qty_up(effective_min * multiplier, pair_info.qty_precision);

    let qty = raw_qty;

    // Determine taker side (opposite of maker)
    let taker_side = if maker_side == "SELL" { "BUY" } else { "SELL" };
    
    // Determine which account is taker (opposite of maker)
    let taker_account = if maker_account == "CMS1" { "CMS2" } else { "CMS1" };

    // Select WS clients for maker and taker
    let (maker_ws, taker_ws) = if maker_account == "CMS1" {
        (cms1_ws, cms2_ws)
    } else {
        (cms2_ws, cms1_ws)
    };

    println!("[INFO] Cycle: {} {} @ {} (maker) vs {} {} @ {} (taker) | Qty: {} | Mid: {}",
        maker_account, maker_side, maker_price, 
        taker_account, taker_side, taker_price, 
        qty, mid_price);

    // STEP 1: Place maker order (regular limit order - will match internally)
    let maker_id = match maker_ws.place_order(
        &pair_info.symbol, maker_side, qty, maker_price,
        false, "GTC",
    ).await {
        Ok(id) => {
            let id_short = &id[..8.min(id.len())];
            println!("[INFO] ✅ {} Maker: {} {} @ {} → {}", maker_account, maker_side, qty, maker_price, id_short);
            liquidator.reset_failure(maker_account, &pair_info.symbol).await;
            id
        }
        Err(e) => {
            let err_str = e.to_string();
            eprintln!("[ERROR] ❌ {} Maker failed: {}", maker_account, e);
            if err_str.contains("Insufficient Balance") || err_str.contains("insufficient") {
                let should_liquidate = liquidator.record_failure(maker_account, &pair_info.symbol).await;
                if should_liquidate {
                    println!("[LIQUIDATE] Auto-liquidation for {} {} ({} failures)",
                        maker_account, pair_info.symbol, FAILURE_THRESHOLD);
                    let _ = liquidator.maybe_liquidate(
                        maker_account, &pair_info.symbol,
                        &pair_info.base_currency, &pair_info.quote_currency,
                        pair_info.price_precision
                    ).await;
                }
            }
            return false;
        }
    };

    // STEP 2: Place taker order (regular limit order - will match internally with maker)
    // Both orders at same price ensures they match with each other
    tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
    
    let taker_result = taker_ws.place_order(
        &pair_info.symbol, taker_side, qty, taker_price,
        false, "GTC",
    ).await;

    // STEP 3: Wait for taker to fill against maker
    // Give enough time for the wash trade to complete
    tokio::time::sleep(tokio::time::Duration::from_millis(100)).await;

    // STEP 4: Only cancel orders that are older than 500ms to avoid cancelling active orders
    // This prevents us from cancelling the maker order before it can fill
    // The cleanup task handles stale orders separately

    // Report result
    match taker_result {
        Ok(taker_id) => {
            let tid_short = &taker_id[..8.min(taker_id.len())];
            println!("[INFO] ✅ {} Taker: {} {} @ {} → {}", taker_account, taker_side, qty, taker_price, tid_short);
            true
        }
        Err(e) => {
            let err_str = e.to_string();
            eprintln!("[ERROR] ❌ {} Taker failed: {}", taker_account, e);
            // Track taker failures too (not just maker)
            if err_str.contains("Insufficient Balance") || err_str.contains("insufficient") {
                let should_liquidate = liquidator.record_failure(taker_account, &pair_info.symbol).await;
                if should_liquidate {
                    println!("[LIQUIDATE] Auto-liquidation for {} {} ({} failures)",
                        taker_account, pair_info.symbol, FAILURE_THRESHOLD);
                    let _ = liquidator.maybe_liquidate(
                        taker_account, &pair_info.symbol,
                        &pair_info.base_currency, &pair_info.quote_currency,
                        pair_info.price_precision
                    ).await;
                }
            }
            false
        }
    }
}

// ── Main ──────────────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() -> Result<()> {
    println!("[INFO] 🤖 CM Bot Spot starting...");

    let config = Config::load(CONFIG_PATH)?;
    let enabled_pairs = config.enabled_pairs();

    if enabled_pairs.is_empty() {
        eprintln!("[ERROR] No enabled pairs in config.json");
        std::process::exit(1);
    }
    println!("[INFO] Enabled pairs: {:?}", enabled_pairs);

    // Load credentials from environment (main account for subaccount impersonation)
    println!("[INFO] Loading credentials from environment...");
    let key = std::env::var("MAIN_API_KEY").context("MAIN_API_KEY not set")?;
    let secret = std::env::var("MAIN_API_SECRET").context("MAIN_API_SECRET not set")?;
    let cms1_id = std::env::var("CM1_SUBACCOUNT_ID").ok().filter(|s| !s.is_empty())
        .context("CM1_SUBACCOUNT_ID not set")?;
    let cms2_id = std::env::var("CM2_SUBACCOUNT_ID").ok().filter(|s| !s.is_empty())
        .context("CM2_SUBACCOUNT_ID not set")?;

    // Fetch pair info
    let mut pair_infos: HashMap<String, PairInfo> = HashMap::new();
    for sym in &enabled_pairs {
        match fetch_pair_info(sym).await {
            Ok(info) => { pair_infos.insert(sym.clone(), info); }
            Err(e) => {
                eprintln!("[ERROR] Failed to fetch pair info for {}: {}", sym, e);
                std::process::exit(1);
            }
        }
    }

    // Use first pair's currencies for WS balance tracking
    let first_pair = pair_infos.values().next().unwrap();
    let base_currency = first_pair.base_currency.clone();
    let quote_currency = first_pair.quote_currency.clone();

    // Shared balance state
    let cms1_balance: SharedBalance = Arc::new(RwLock::new(SpotBalance::default()));
    let cms2_balance: SharedBalance = Arc::new(RwLock::new(SpotBalance::default()));

    // Start account WebSocket clients
    println!("[INFO] Connecting WS for CMS1...");
    let cms1_ws = new_ws_client(
        key.clone(), secret.clone(), cms1_id.clone(),
        Arc::clone(&cms1_balance), "CMS1".to_string(),
        base_currency.clone(), quote_currency.clone(),
    );

    println!("[INFO] Connecting WS for CMS2...");
    let cms2_ws = new_ws_client(
        key.clone(), secret.clone(), cms2_id.clone(),
        Arc::clone(&cms2_balance), "CMS2".to_string(),
        base_currency.clone(), quote_currency.clone(),
    );

    // Start price feed WebSocket
    let price_feed = PriceFeed::new(WS_TRADE_URL);
    let symbols_ref: Vec<&str> = enabled_pairs.iter().map(|s| s.as_str()).collect();
    if let Err(e) = price_feed.connect_and_subscribe(&symbols_ref).await {
        eprintln!("[WARN] Price feed WS failed: {}", e);
    }

    // Wait for WS connections
    tokio::time::sleep(tokio::time::Duration::from_secs(3)).await;

    // Load state
    let mut state = load_state(STATE_PATH);

    // REST client for cleanup
    let http = reqwest::Client::new();

    // Cleanup task
    let cleanup_http = http.clone();
    let cleanup_key = key.clone();
    let cleanup_secret = secret.clone();
    let cleanup_cms1 = cms1_id.clone();
    let cleanup_cms2 = cms2_id.clone();
    let cleanup_pairs = enabled_pairs.clone();
    let cleanup_threshold = config.cleanup_age_threshold_ms;
    let cleanup_interval = config.cleanup_interval_ms;
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(tokio::time::Duration::from_millis(cleanup_interval)).await;
            cancel_stale_orders(&cleanup_http, &cleanup_key, &cleanup_secret, &cleanup_cms1,
                "CMS1", &cleanup_pairs, cleanup_threshold).await;
            cancel_stale_orders(&cleanup_http, &cleanup_key, &cleanup_secret, &cleanup_cms2,
                "CMS2", &cleanup_pairs, cleanup_threshold).await;
        }
    });

    // Initialise rebalancer
    let rebalancer = rebalance::Rebalancer::new(
        key.clone(),
        secret.clone(),
        cms1_id.parse::<u64>().unwrap_or(0),
        cms2_id.parse::<u64>().unwrap_or(0),
    );

    // Initialise liquidator
    let liquidator = liquidator::Liquidator::new(
        key.clone(),
        secret.clone(),
        cms1_id.parse::<u64>().unwrap_or(0),
        cms2_id.parse::<u64>().unwrap_or(0),
    );

    // Collect assets for rebalancing
    let all_assets = rebalancer.collect_assets(pair_infos.values());
    println!("[INFO] Tracking {} assets for rebalancing", all_assets.len());

    // Wrap pair_infos in Arc
    let pair_infos_arc = Arc::new(pair_infos);
    
    // Random maker selector (separate mutex to avoid lock contention)
    let random_maker: Arc<tokio::sync::Mutex<random_maker::RandomMakerSelector>> = 
        Arc::new(tokio::sync::Mutex::new(random_maker::RandomMakerSelector::new()));
    
    // Background rebalancer task
    let rebalancer_clone = rebalancer.clone();
    let assets_clone = all_assets.clone();
    let price_feed_clone = price_feed.clone();
    let pair_infos_arc_clone = pair_infos_arc.clone();
    tokio::spawn(async move {
        println!("[REBALANCE] Background rebalancer started (90s interval)");
        loop {
            tokio::time::sleep(tokio::time::Duration::from_secs(90)).await;
            
            let mut assets_with_prices = assets_clone.clone();
            for pair_info in pair_infos_arc_clone.values() {
                if let Some(prices) = price_feed_clone.get_orderbook(&pair_info.symbol).await {
                    if let Some(asset) = assets_with_prices.get_mut(&pair_info.base_currency) {
                        asset.price_usd = prices.mid;
                    }
                }
            }
            
            println!("[REBALANCE] Running asset rebalance...");
            rebalancer_clone.run(&assets_with_prices).await;
        }
    });

    // Global cycle count for synchronized rotation across all pairs
    let mut global_cycle_count: u64 = 0;

    println!("[INFO] Starting cycle loop (interval: {}ms)", config.cycle_interval_ms);

    loop {
        // Log balances
        {
            let b1 = cms1_balance.read().await;
            let b2 = cms2_balance.read().await;
            println!("[INFO] Balance: CMS1 ZAR={:.2} {}={:.4} | CMS2 ZAR={:.2} {}={:.4}",
                b1.zar_available, base_currency, b1.base_available,
                b2.zar_available, base_currency, b2.base_available);
        }

        let mut any_success = false;
        for sym in &enabled_pairs {
            let pair_info = match pair_infos_arc.get(sym) {
                Some(p) => p,
                None => continue,
            };

            // Get price from WS orderbook
            let prices = match price_feed.get_orderbook(sym).await {
                Some(p) => p,
                None => {
                    eprintln!("[WARN] No price data for {} yet, skipping", sym);
                    continue;
                }
            };

            // ═══════════════════════════════════════════════════════════════
            // RANDOMIZED MAKER SELECTION (organic-looking trade patterns)
            // ═══════════════════════════════════════════════════════════════
            // Uses Option B: Random with balance tracking
            // - Tracks last 10 cycles, biases toward underrepresented account
            // - Hard cap: max 5 consecutive same-side cycles
            // This makes wash trades look like organic two-way trading
            // ═══════════════════════════════════════════════════════════════
            
            // Select maker randomly (true = CMS1, false = CMS2) AND randomize side
            let (is_cms1_maker, maker_sells, cms1_count, cms2_count) = {
                let mut selector = random_maker.lock().await;
                let is_cms1 = selector.select_maker();
                let sells = selector.select_maker_side(); // true = maker sells (taker buys), false = maker buys (taker sells)
                let (c1, c2, _) = selector.get_stats();
                (is_cms1, sells, c1, c2)
            };
            
            let maker_side = if maker_sells { "SELL" } else { "BUY" };
            let taker_side = if maker_sells { "BUY" } else { "SELL" };
            let (maker_account, taker_account) = if is_cms1_maker {
                ("CMS1", "CMS2")
            } else {
                ("CMS2", "CMS1")
            };

            println!("[INFO] 🎲 Maker: {} {} vs {} {} | Last 10: CMS1={}, CMS2={} | Cycle #{}", 
                maker_account, maker_side, taker_account, taker_side, cms1_count, cms2_count, global_cycle_count);

            // Pre-cycle: check for systemic base surplus (both accounts heavy on base)
            // This requires selling base, not just transferring
            let _ = rebalancer.correct_base_asset_surplus(
                &pair_info.base_currency,
                &pair_info.quote_currency,
                &pair_info.symbol,
                prices.mid,
            ).await;

            // Pre-cycle inventory check: rebalance if maker is low on base or taker is low on quote
            let maker_is_selling = maker_sells;
            let rebalanced = rebalancer.rebalance_pair_if_needed(
                &pair_info.base_currency,
                &pair_info.quote_currency,
                maker_account,
                if maker_account == "CMS1" { "CMS2" } else { "CMS1" },
                maker_is_selling,
            ).await;
            
            // Small delay to let transfer propagate if rebalance was triggered
            if rebalanced {
                tokio::time::sleep(tokio::time::Duration::from_millis(500)).await;
            }

            // Pre-cycle cleanup: cancel any existing orders for this pair
            let _ = cancel_all_orders_for_pair(&http, &key, &secret, &cms1_id, sym).await;
            let _ = cancel_all_orders_for_pair(&http, &key, &secret, &cms2_id, sym).await;

            let success = execute_cycle(
                &http, &key, &secret, &cms1_id, &cms2_id,
                &cms1_ws, &cms2_ws,
                pair_info, &prices,
                &cms1_balance, &cms2_balance,
                maker_account, maker_side,
                config.qty_range_min_multiplier,
                config.qty_range_max_multiplier,
                &liquidator,
            ).await;

            if success {
                any_success = true;
                let mut updated_state = None;
                {
                    let pair_state = state.entry(sym.clone()).or_default();
                    pair_state.cycle_count += 1;
                    pair_state.total_trades += 1;
                    updated_state = Some(pair_state.cycle_count);
                }
                if let Some(new_count) = updated_state {
                    save_state(STATE_PATH, &state);
                    println!("[INFO] Cycle recorded for {}: cycle #{}", sym, new_count);
                }
            }
        }

        if any_success {
            global_cycle_count += 1;
        }

        tokio::time::sleep(tokio::time::Duration::from_millis(config.cycle_interval_ms)).await;
    }
}
