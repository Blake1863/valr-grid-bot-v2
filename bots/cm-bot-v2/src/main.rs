mod config;
mod pair_info;
mod types;
mod valr;
mod state;
mod cycle;
mod cleanup;
mod price_feed;
mod ws_client;
mod random_maker;

use config::Config;
use state::StateManager;
use types::OrderSide;
use valr::{ValrClient, SharedMarginState, MarginState};
use ws_client::WsClient;
use price_feed::PriceFeed;
use std::sync::Arc;
use tokio::sync::RwLock;
use std::env;
use std::process::Command;
use anyhow::{Result, Context};

const CONFIG_PATH: &str = "config.json";
const STATE_PATH: &str = "state.json";
const API_BASE_URL: &str = "https://api.valr.com";
const WS_TRADE_URL: &str = "wss://api.valr.com/ws/trade";

const MIN_BALANCE: f64 = 2.0;  // Minimum availableInReference in USDT

/// Load a secret from the encrypted vault via secrets.py
fn load_secret(name: &str) -> Result<String> {
    let output = Command::new("python3")
        .args(["/home/admin/.openclaw/secrets/secrets.py", "get", name])
        .output()
        .context(format!("Failed to read {}", name))?;
    Ok(String::from_utf8(output.stdout)?.trim().to_string())
}

struct AppState {
    config: Config,
    pair_infos: Vec<pair_info::PairInfo>,
    state_manager: StateManager,
    cm1_client: ValrClient,   // REST client — used only for balance fallback + cleanup
    cm2_client: ValrClient,
    cm1_ws: WsClient,         // WS client — used for all order placement
    cm2_ws: WsClient,
    price_feed: PriceFeed,
    margin_state: SharedMarginState,
}

type SharedRandomMaker = Arc<tokio::sync::Mutex<random_maker::RandomMakerSelector>>;

#[tokio::main]
async fn main() -> Result<()> {
    // Initialize logging
    tracing_subscriber::fmt::init();
    
    println!("[INFO] 🤖 Chart Maintenance Bot v2 starting...");
    
    // Load config
    let config = Config::load(CONFIG_PATH)?;
    println!("[INFO] Loaded config with {} pairs", config.pairs.len());
    
    // Load API keys from secrets manager (CM account for subaccount impersonation)
    println!("[INFO] Loading credentials from secrets manager...");
    let cm1_key = load_secret("valr_main_api_key")?;
    let cm1_secret = load_secret("valr_main_api_secret")?;
    let cm2_key = cm1_key.clone();  // Same CM account key
    let cm2_secret = cm1_secret.clone();  // Same CM account secret
    let cm1_subaccount_id = env::var("CM1_SUBACCOUNT_ID").ok().filter(|s| !s.is_empty());
    let cm2_subaccount_id = env::var("CM2_SUBACCOUNT_ID").ok().filter(|s| !s.is_empty());
    
    // Fetch pair info from VALR API (cached, only on startup)
    println!("[INFO] Fetching pair info from VALR API...");
    let pair_infos = pair_info::fetch_all_pair_info(&config, API_BASE_URL, &cm1_key, &cm1_secret, cm1_subaccount_id.as_deref()).await?;
    println!("[INFO] Fetched info for {} enabled pairs", pair_infos.len());
    
    if pair_infos.is_empty() {
        eprintln!("[ERROR] No enabled pairs found!");
        std::process::exit(1);
    }
    
    // Initialize state manager
    let state_manager = StateManager::new(STATE_PATH);
    state_manager.load_state().await;
    
    // Initialize clients
    let cm1_client = ValrClient::new(cm1_key.clone(), cm1_secret.clone(), cm1_subaccount_id.clone(), API_BASE_URL.to_string());
    let cm2_client = ValrClient::new(cm2_key.clone(), cm2_secret.clone(), cm2_subaccount_id.clone(), API_BASE_URL.to_string());
    
    // Initialize price feed
    let price_feed = PriceFeed::new(WS_TRADE_URL);
    
    // Initialize shared margin state (updated by WebSocket, read by cycle loop)
    let margin_state: SharedMarginState = Arc::new(RwLock::new(MarginState::default()));
    
    // Initialize WebSocket clients — used for order placement and balance updates
    println!("[INFO] Connecting WS for CM1...");
    let cm1_ws = WsClient::new(
        cm1_key.clone(),
        cm1_secret.clone(),
        cm1_subaccount_id.clone(),
        Arc::clone(&margin_state),
        "CM1".to_string(),
    ).await;

    println!("[INFO] Connecting WS for CM2...");
    let cm2_ws = WsClient::new(
        cm2_key.clone(),
        cm2_secret.clone(),
        cm2_subaccount_id.clone(),
        Arc::clone(&margin_state),
        "CM2".to_string(),
    ).await;

    // Wait briefly for WS connections to authenticate and receive first BALANCE_UPDATE
    tokio::time::sleep(tokio::time::Duration::from_secs(2)).await;

    // Create separate mutex for random maker (avoids lock contention with main state)
    let random_maker: SharedRandomMaker = Arc::new(tokio::sync::Mutex::new(
        random_maker::RandomMakerSelector::new()
    ));
    
    let state = Arc::new(RwLock::new(AppState {
        config,
        pair_infos,
        state_manager,
        cm1_client,
        cm2_client,
        cm1_ws,
        cm2_ws,
        price_feed,
        margin_state,
    }));
    
    // Connect price feed
    {
        let state = state.read().await;
        let symbols: Vec<&str> = state.pair_infos.iter().map(|p| p.symbol.as_str()).collect();
        if let Err(e) = state.price_feed.connect_and_subscribe(&symbols).await {
            eprintln!("[WARN] Price feed connection failed: {}", e);
        }
    }
    
    // Spawn cleanup task (every 60s)
    let cleanup_state = Arc::clone(&state);
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(tokio::time::Duration::from_millis(
                cleanup_state.read().await.config.cleanup_interval_ms
            )).await;
            
            let state = cleanup_state.read().await;
            let symbols: Vec<&str> = state.pair_infos.iter().map(|p| p.symbol.as_str()).collect();
            
            // Clean stale orders on BOTH accounts
            cleanup::cleanup_stale_orders(
                &state.cm1_client,
                &symbols,
                state.config.cleanup_age_threshold_ms,
            ).await;
            cleanup::cleanup_stale_orders(
                &state.cm2_client,
                &symbols,
                state.config.cleanup_age_threshold_ms,
            ).await;
        }
    });
    
    // Main cycle loop
    let cycle_interval = state.read().await.config.cycle_interval_ms;
    println!("[INFO] Starting cycle loop (interval: {}ms)", cycle_interval);
    println!("[INFO] Min balance: ${} | Rotation: Full flip every 3 cycles, 6-cycle net=0 (90s)", MIN_BALANCE);
    
    loop {
        let state = state.read().await;
        
        // Balance check: prefer WebSocket, fallback to REST
        let margin = state.margin_state.read().await;
        let mut cm1_balance = margin.cm1_available;
        let mut cm2_balance = margin.cm2_available;
        drop(margin);
        
        // WS only pushes BALANCE_UPDATE on change — always fall back to REST if no WS data.
        // The account WS closes immediately after subscribe (server pushes on change, not on connect),
        // so cm1_balance/cm2_balance from WS will typically be 0 and REST is used every cycle.
        if cm1_balance < 0.01 {
            cm1_balance = state.cm1_client.get_available_in_reference().await.unwrap_or(0.0);
        }
        if cm2_balance < 0.01 {
            cm2_balance = state.cm2_client.get_available_in_reference().await.unwrap_or(0.0);
        }
        
        println!("[INFO] Balance: CM1=${:.2} | CM2=${:.2}", cm1_balance, cm2_balance);
        
        if cm1_balance < MIN_BALANCE || cm2_balance < MIN_BALANCE {
            eprintln!("[WARN] Balance below ${} - pausing for 60s", MIN_BALANCE);
            drop(state);
            tokio::time::sleep(tokio::time::Duration::from_secs(60)).await;
            continue;
        }
        
        for pair_info in &state.pair_infos {
            // Fetch live orderbook prices (bid/ask/mid) for accurate maker placement
            let prices = match state.price_feed.get_orderbook(&pair_info.symbol).await {
                Some(p) => p,
                None => {
                    match price_feed::fetch_orderbook_prices(&pair_info.symbol, API_BASE_URL).await {
                        Ok(p) => p,
                        Err(e) => {
                            eprintln!("[ERROR] Failed to get price for {}: {}", pair_info.symbol, e);
                            continue;
                        }
                    }
                }
            };
            
            // Get cycle count
            let cycle_count = state.state_manager.get_cycle_count(&pair_info.symbol).await;
            
            // RANDOMIZED MAKER SELECTION (organic-looking trade patterns)
            // Uses Option B: Random with balance tracking
            // - Tracks last 10 cycles, biases toward underrepresented account
            // - Hard cap: max 5 consecutive same-side cycles
            // This makes wash trades look like organic two-way trading
            let (is_cm1_maker, maker_sells, cms1, cms2) = {
                let mut selector = random_maker.lock().await;
                let is_cm1 = selector.select_maker();
                let sells = selector.select_maker_side(); // true = maker sells, false = maker buys
                let (c1, c2, _) = selector.get_stats();
                (is_cm1, sells, c1, c2)
            };
            
            // Randomize both WHICH account is maker AND whether maker buys or sells
            // This makes taker alternate between buy and sell in trade history
            let maker_side = if maker_sells { OrderSide::Sell } else { OrderSide::Buy };
            let (maker_account, taker_account) = if is_cm1_maker {
                ("CM1", "CM2")
            } else {
                ("CM2", "CM1")
            };
            
            // Skip cycle if taker account has no available balance
            let taker_balance = if maker_account == "CM1" { cm2_balance } else { cm1_balance };
            if taker_balance < MIN_BALANCE {
                state.state_manager.record_cycle(&pair_info.symbol, false).await;
                let _ = state.state_manager.save_state().await;
                eprintln!("[WARN] Taker balance ${:.2} < ${} for {} — advancing cycle", 
                    taker_balance, MIN_BALANCE, pair_info.symbol);
                continue;
            }
            
            println!("[INFO] 🎲 Maker: {} {:?} vs {} {:?} | Last 10: CM1={}, CM2={} | Cycle {}", 
                maker_account, maker_side, 
                taker_account, maker_side.opposite(),
                cms1, cms2,
                cycle_count);
            
            // Select WS clients for order placement
            let maker_ws = if maker_account == "CM1" { &state.cm1_ws } else { &state.cm2_ws };
            let taker_ws = if taker_account == "CM1" { &state.cm1_ws } else { &state.cm2_ws };

            // REST clients still used by cleanup task — not needed here
            let result = cycle::execute_cycle_with_qty_range(
                maker_ws,
                taker_ws,
                pair_info,
                &prices,
                &maker_account,
                &taker_account,
                maker_side,
                state.config.qty_range_min_multiplier,
                state.config.qty_range_max_multiplier,
            ).await;
            
            if result.success {
                state.state_manager.record_cycle(&pair_info.symbol, result.external_fill).await;
                let _ = state.state_manager.save_state().await;
            }
        }
        
        drop(state);
        
        // Wait for next cycle
        tokio::time::sleep(tokio::time::Duration::from_millis(cycle_interval)).await;
    }
}
