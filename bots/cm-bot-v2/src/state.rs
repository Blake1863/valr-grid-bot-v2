use crate::types::{OrderSide, PairState};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs;
use std::path::Path;
use std::sync::Arc;
use tokio::sync::RwLock;
use anyhow::Result;

#[derive(Debug, Serialize, Deserialize)]
struct SerializablePairState {
    cycle_count: u64,
    current_phase: u64,
    maker_account: String,
    maker_side: String,
    last_cycle_time: String,
    total_trades: u64,
    external_fills: u64,
}

impl From<&PairState> for SerializablePairState {
    fn from(state: &PairState) -> Self {
        Self {
            cycle_count: state.cycle_count,
            current_phase: state.current_phase,
            maker_account: state.maker_account.clone(),
            maker_side: state.maker_side.to_string(),
            last_cycle_time: state.last_cycle_time.to_rfc3339(),
            total_trades: state.total_trades,
            external_fills: state.external_fills,
        }
    }
}

impl Into<PairState> for SerializablePairState {
    fn into(self) -> PairState {
        PairState {
            cycle_count: self.cycle_count,
            current_phase: self.current_phase,
            maker_account: self.maker_account,
            maker_side: match self.maker_side.as_str() {
                "SELL" => OrderSide::Sell,
                _ => OrderSide::Buy,
            },
            last_cycle_time: chrono::DateTime::parse_from_rfc3339(&self.last_cycle_time)
                .unwrap_or_else(|_| chrono::DateTime::parse_from_rfc3339(&chrono::Utc::now().to_rfc3339()).unwrap())
                .with_timezone(&chrono::Utc),
            total_trades: self.total_trades,
            external_fills: self.external_fills,
        }
    }
}

#[derive(Clone)]
pub struct StateManager {
    state_file: String,
    pair_states: Arc<RwLock<HashMap<String, PairState>>>,
}

impl StateManager {
    pub fn new(state_file: &str) -> Self {
        Self {
            state_file: state_file.to_string(),
            pair_states: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    pub async fn load_state(&self) {
        if Path::new(&self.state_file).exists() {
            match fs::read_to_string(&self.state_file) {
                Ok(content) => {
                    match serde_json::from_str::<HashMap<String, SerializablePairState>>(&content) {
                        Ok(data) => {
                            let mut states = self.pair_states.write().await;
                            *states = data
                                .into_iter()
                                .map(|(k, v)| (k, v.into()))
                                .collect();
                            println!("[INFO] Loaded state for {} pairs", states.len());
                        }
                        Err(e) => eprintln!("[WARN] Failed to parse state file: {}", e),
                    }
                }
                Err(e) => eprintln!("[WARN] Failed to read state file: {}", e),
            }
        } else {
            println!("[INFO] No existing state file, starting fresh");
        }
    }

    pub async fn save_state(&self) -> Result<()> {
        let states = self.pair_states.read().await;
        let serializable: HashMap<String, SerializablePairState> = states
            .iter()
            .map(|(k, v)| (k.clone(), v.into()))
            .collect();

        let content = serde_json::to_string_pretty(&serializable)?;
        
        // Atomic write: write to temp file, then rename
        let temp_file = format!("{}.tmp", self.state_file);
        fs::write(&temp_file, &content)?;
        fs::rename(&temp_file, &self.state_file)?;
        
        println!("[INFO] State saved");
        Ok(())
    }

    pub async fn get_maker_account(&self, symbol: &str, cycles_before_switch: u64) -> String {
        let states = self.pair_states.read().await;
        let state = states.get(symbol);
        match state {
            Some(s) => {
                let phase = s.cycle_count / cycles_before_switch;
                if phase % 2 == 0 { "CM1".to_string() } else { "CM2".to_string() }
            }
            None => "CM1".to_string(),
        }
    }

    pub async fn get_maker_side(&self, symbol: &str) -> OrderSide {
        let states = self.pair_states.read().await;
        let state = states.get(symbol);
        match state {
            Some(s) => {
                if s.cycle_count % 2 == 0 { OrderSide::Buy } else { OrderSide::Sell }
            }
            None => OrderSide::Buy,
        }
    }

    pub async fn record_cycle(&self, symbol: &str, external_fill: bool) {
        let mut states = self.pair_states.write().await;
        let state = states.entry(symbol.to_string()).or_insert_with(PairState::default);
        state.cycle_count += 1;
        state.last_cycle_time = chrono::Utc::now();
        state.total_trades += 1;
        state.current_phase = (state.cycle_count / 3) % 2;
        
        if external_fill {
            state.external_fills += 1;
        }
        
        println!("[INFO] Cycle recorded for {}: cycle #{}", symbol, state.cycle_count);
    }

    pub async fn get_cycle_count(&self, symbol: &str) -> u64 {
        let states = self.pair_states.read().await;
        states.get(symbol).map(|s| s.cycle_count).unwrap_or(0)
    }
}
