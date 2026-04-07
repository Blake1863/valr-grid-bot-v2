use crate::config::PairConfig;
use crate::types::PairType;
use crate::valr::ValrPairResponse;
use anyhow::Result;
use std::collections::HashMap;

#[derive(Debug, Clone)]
pub struct PairInfo {
    pub symbol: String,
    pub pair_type: PairType,
    pub price_precision: u8,
    pub qty_precision: u8,
    pub min_qty: f64,
    pub min_value: f64,
    pub enabled: bool,
}

/// Convert tick size string to price precision
/// e.g., "1" -> 0, "0.01" -> 2, "0.0001" -> 4
fn tick_size_to_precision(tick_size: &str) -> u8 {
    if let Ok(num) = tick_size.parse::<f64>() {
        if num == 1.0 {
            return 0;
        }
        let s = tick_size.trim_end_matches('0');
        if let Some(decimal_pos) = s.find('.') {
            return (s.len() - decimal_pos - 1) as u8;
        }
    }
    0
}

/// Fetch all pairs from VALR API and filter to our enabled list
pub async fn fetch_all_pair_info(
    config: &crate::config::Config,
    api_base_url: &str,
    api_key: &str,
    api_secret: &str,
    subaccount_id: Option<&str>,
) -> Result<Vec<PairInfo>> {
    use crate::valr::ValrClient;
    
    let client = ValrClient::new(
        api_key.to_string(),
        api_secret.to_string(),
        subaccount_id.map(String::from),
        api_base_url.to_string(),
    );
    
    // Fetch all pairs from VALR public endpoint
    let all_pairs: Vec<ValrPairResponse> = client.get_all_pairs().await?;
    let pairs_map: HashMap<String, ValrPairResponse> = all_pairs
        .into_iter()
        .map(|p| (p.symbol.clone(), p))
        .collect();
    
    let mut pair_infos = Vec::new();
    
    for (symbol, pair_config) in &config.pairs {
        if !pair_config.enabled {
            continue;
        }
        
        if let Some(valr_pair) = pairs_map.get(symbol) {
            // Convert tick size to price precision
            let price_precision = tick_size_to_precision(&valr_pair.tick_size);
            
            // Parse min quantity
            let min_qty = valr_pair.min_base_amount.parse::<f64>()
                .unwrap_or(0.001); // fallback
            
            // Parse base decimal places (quantity precision)
            let qty_precision = valr_pair.base_decimal_places.parse::<u8>()
                .unwrap_or(8); // fallback
            
            // Determine min_value from API or sensible defaults
            // Priority: config override > API minQuoteAmount > currency-based default
            let min_value = if let Some(override_val) = pair_config.min_value_override {
                override_val
            } else {
                // Try to parse minQuoteAmount from API
                let api_min = valr_pair.min_quote_amount.parse::<f64>().ok();
                
                if let Some(api_val) = api_min {
                    api_val
                } else {
                    // Fallback: sensible defaults based on quote currency
                    let quote_currency = symbol.chars()
                        .rev()
                        .take_while(|c| c.is_alphabetic())
                        .collect::<String>()
                        .chars()
                        .rev()
                        .collect::<String>();
                    
                    match quote_currency.as_str() {
                        "ZAR" => 10.0,      // 10 ZAR minimum
                        "USDT" | "USDC" => 0.5,  // 0.5 USDT/USDC minimum
                        _ => 5.0,           // Generic fallback
                    }
                }
            };
            
            let info = PairInfo {
                symbol: valr_pair.symbol.clone(),
                pair_type: pair_config.pair_type,
                price_precision,
                qty_precision: qty_precision,
                min_qty,
                min_value,
                enabled: true,
            };
            
            println!("[INFO] Fetched pair info for {}: price_precision={}, qty_precision={}, min_qty={}, min_value={}", 
                symbol, info.price_precision, info.qty_precision, info.min_qty, info.min_value);
            pair_infos.push(info);
        } else {
            eprintln!("[WARN] Pair {} not found in VALR API response", symbol);
        }
    }
    
    Ok(pair_infos)
}