use serde::Deserialize;
use std::collections::HashMap;
use crate::types::PairType;
use anyhow::Result;

#[derive(Debug, Clone, Deserialize)]
pub struct Config {
    pub pairs: HashMap<String, PairConfig>,
    #[serde(default = "default_cycle_interval")]
    pub cycle_interval_ms: u64,
    #[serde(default = "default_cleanup_interval")]
    pub cleanup_interval_ms: u64,
    #[serde(default = "default_qty_range_min")]
    pub qty_range_min_multiplier: f64,
    #[serde(default = "default_qty_range_max")]
    pub qty_range_max_multiplier: f64,
    #[serde(default = "default_cycles_before_switch")]
    pub cycles_before_role_switch: u64,
    #[serde(default = "default_inventory_buffer")]
    pub inventory_buffer_cycles: u64,
    #[serde(default = "default_cleanup_threshold")]
    pub cleanup_age_threshold_ms: u64,
}

fn default_cycle_interval() -> u64 { 15000 }
fn default_cleanup_interval() -> u64 { 60000 }
fn default_qty_range_min() -> f64 { 1.1 }
fn default_qty_range_max() -> f64 { 3.0 }
fn default_cycles_before_switch() -> u64 { 3 }
fn default_inventory_buffer() -> u64 { 4 }
fn default_cleanup_threshold() -> u64 { 30000 }

#[derive(Debug, Clone, Deserialize)]
pub struct PairConfig {
    pub enabled: bool,
    #[serde(rename = "type")]
    pub pair_type: PairType,
    pub min_value_override: Option<f64>,
}

impl Config {
    pub fn load(path: &str) -> Result<Self> {
        let content = std::fs::read_to_string(path)
            .map_err(|e| anyhow::anyhow!("Failed to read config file '{}': {}", path, e))?;
        let config: Config = serde_json::from_str(&content)
            .map_err(|e| anyhow::anyhow!("Failed to parse config JSON: {}", e))?;
        Ok(config)
    }

    pub fn enabled_pairs(&self) -> Vec<&String> {
        self.pairs
            .iter()
            .filter(|(_, cfg)| cfg.enabled)
            .map(|(symbol, _)| symbol)
            .collect()
    }
}
