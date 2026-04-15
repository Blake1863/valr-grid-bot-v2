/// Inventory rebalancer — asset-focused, runs every N cycles.
///
/// Collects all assets (base + quote) used by enabled pairs,
/// fetches total balances from CMS1 and CMS2, and rebalances
/// any asset where one account holds >60% of combined total.
///
/// Transfers aim for 50/50 split across both accounts.
use anyhow::{Context, Result};
use serde::Deserialize;
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

/// Trigger a rebalance when one account holds more than this fraction of the total.
/// 0.60 = rebalance if either account has >60% of combined holdings.
const REBALANCE_THRESHOLD_PCT: f64 = 0.60;

/// Don't bother transferring less than this USD-equivalent value.
const MIN_TRANSFER_VALUE_USD: f64 = 1.0;

#[derive(Debug, Deserialize)]
struct ValrBalance {
    currency: String,
    available: String,
}

/// Asset metadata for rebalancing
#[derive(Debug, Clone)]
pub struct AssetInfo {
    /// Currency code as it appears in balance API (e.g., "R" for ZAR)
    pub balance_key: String,
    /// Currency code for transfer API (e.g., "ZAR" for ZAR)
    pub transfer_code: String,
    /// Approximate USD price for minimum value check
    pub price_usd: f64,
    /// Decimal places for this asset
    pub dp: usize,
}

#[derive(Clone)]
pub struct Rebalancer {
    api_key: String,
    api_secret: String,
    cms1_id: u64,
    cms2_id: u64,
    client: reqwest::Client,
}

impl Rebalancer {
    pub fn new(api_key: String, api_secret: String, cms1_id: u64, cms2_id: u64) -> Self {
        Self {
            api_key,
            api_secret,
            cms1_id,
            cms2_id,
            client: reqwest::Client::new(),
        }
    }

    fn timestamp_ms() -> u64 {
        SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64
    }

    fn sign(&self, method: &str, path: &str, body: &str, subaccount: u64) -> (u64, String) {
        use hmac::{Hmac, Mac};
        use sha2::Sha512;
        let ts = Self::timestamp_ms();
        let sub_str = if subaccount == 0 { String::new() } else { subaccount.to_string() };
        let msg = format!("{}{}{}{}{}", ts, method.to_uppercase(), path, body, sub_str);
        let mut mac = Hmac::<Sha512>::new_from_slice(self.api_secret.as_bytes()).unwrap();
        mac.update(msg.as_bytes());
        (ts, hex::encode(mac.finalize().into_bytes()))
    }

    async fn get_balances(&self, subaccount: u64) -> Result<HashMap<String, f64>> {
        let path = "/v1/account/balances";
        let (ts, sig) = self.sign("GET", path, "", subaccount);

        let resp = self.client
            .get(format!("https://api.valr.com{}", path))
            .header("X-VALR-API-KEY", &self.api_key)
            .header("X-VALR-SIGNATURE", sig)
            .header("X-VALR-TIMESTAMP", ts.to_string())
            .header("X-VALR-SUB-ACCOUNT-ID", subaccount.to_string())
            .send().await
            .context("Failed to fetch balances")?;

        let balances: Vec<ValrBalance> = resp.json().await
            .context("Failed to parse balances")?;

        Ok(balances.into_iter()
            .filter_map(|b| {
                let avail = b.available.parse::<f64>().ok()?;
                if avail > 0.0 { Some((b.currency, avail)) } else { None }
            })
            .collect())
    }

    async fn transfer(&self, currency: &str, amount: f64, from: u64, to: u64, dp: usize) -> Result<()> {
        let path = "/v1/account/subaccounts/transfer";
        let body = serde_json::json!({
            "fromId": from,
            "toId": to,
            "currencyCode": currency,
            "amount": format!("{:.prec$}", amount, prec = dp),
            "allowBorrow": false
        });
        let body_str = serde_json::to_string(&body)?;
        let (ts, sig) = self.sign("POST", path, &body_str, 0);

        let resp = self.client
            .post(format!("https://api.valr.com{}", path))
            .header("X-VALR-API-KEY", &self.api_key)
            .header("X-VALR-SIGNATURE", sig)
            .header("X-VALR-TIMESTAMP", ts.to_string())
            .header("Content-Type", "application/json")
            .body(body_str)
            .send().await
            .context("Failed to send transfer")?;

        let status = resp.status();
        if status.is_success() || status.as_u16() == 202 {
            Ok(())
        } else {
            let text = resp.text().await.unwrap_or_default();
            anyhow::bail!("Transfer failed ({}): {}", status, text)
        }
    }

    /// Collect all unique assets from enabled pairs.
    /// Each pair contributes its base and quote currency.
    /// Returns a set of AssetInfo for each unique asset.
    pub fn collect_assets<'a>(
        &self,
        pair_infos: impl Iterator<Item = &'a crate::PairInfo>,
    ) -> HashMap<String, AssetInfo> {
        let mut assets: HashMap<String, AssetInfo> = HashMap::new();

        for pair_info in pair_infos {
            // Add base asset
            let base = pair_info.base_currency.clone();
            if !assets.contains_key(&base) {
                assets.insert(base.clone(), AssetInfo {
                    balance_key: base.clone(),
                    transfer_code: base.clone(),
                    price_usd: 0.0, // Will be updated from price feed
                    dp: pair_info.qty_precision as usize,
                });
            }

            // Add quote asset (handle ZAR specially)
            let quote = pair_info.quote_currency.clone();
            // VALR returns "ZAR" in balance API, not "R" - "R" is only used in WS balance updates
            let (quote_key, quote_code) = if quote == "ZAR" {
                ("ZAR".to_string(), "ZAR".to_string())
            } else {
                (quote.clone(), quote.clone())
            };

            if !assets.contains_key(&quote_key) {
                // ZAR has special pricing
                let quote_price = if quote == "ZAR" { 0.055 } else { 1.0 };
                assets.insert(quote_key.clone(), AssetInfo {
                    balance_key: quote_key,
                    transfer_code: quote_code,
                    price_usd: quote_price,
                    dp: 2, // Quote assets typically 2 dp
                });
            }
        }

        assets
    }

    /// Run a full asset-focused rebalance pass.
    /// Fetches all balances once, then rebalances each asset independently
    /// where one account holds >60% of the combined total.
    pub async fn run(&self, assets: &HashMap<String, AssetInfo>) {
        let bal1 = match self.get_balances(self.cms1_id).await {
            Ok(b) => b,
            Err(e) => { eprintln!("[REBALANCE] Failed to fetch CMS1 balances: {}", e); return; }
        };
        let bal2 = match self.get_balances(self.cms2_id).await {
            Ok(b) => b,
            Err(e) => { eprintln!("[REBALANCE] Failed to fetch CMS2 balances: {}", e); return; }
        };

        println!("[REBALANCE] Checking {} assets for rebalancing...", assets.len());

        for (_asset_key, asset_info) in assets {
            let c1 = bal1.get(&asset_info.balance_key).copied().unwrap_or(0.0);
            let c2 = bal2.get(&asset_info.balance_key).copied().unwrap_or(0.0);
            let total = c1 + c2;

            if total < 1e-8 {
                continue; // No holdings of this asset
            }

            let target = total / 2.0;
            let c1_pct = c1 / total;
            let c2_pct = c2 / total;

            // Check if either account holds more than threshold
            let imbalanced = c1_pct > REBALANCE_THRESHOLD_PCT || c2_pct > REBALANCE_THRESHOLD_PCT;
            if !imbalanced {
                continue;
            }

            // Determine transfer direction
            let (from, to, from_label, to_label, surplus) = if c1 > c2 {
                (self.cms1_id, self.cms2_id, "CMS1", "CMS2", c1 - target)
            } else {
                (self.cms2_id, self.cms1_id, "CMS2", "CMS1", c2 - target)
            };

            // Round down to dp precision
            let factor = 10f64.powi(asset_info.dp as i32);
            let transfer_amt = (surplus * factor).floor() / factor;

            // Skip tiny transfers
            if transfer_amt * asset_info.price_usd < MIN_TRANSFER_VALUE_USD {
                continue;
            }

            println!("[REBALANCE] {} imbalanced: CMS1={:.4} CMS2={:.4} → transfer {:.prec$} {} from {} to {}",
                asset_info.transfer_code, c1, c2, transfer_amt, asset_info.transfer_code, from_label, to_label,
                prec = asset_info.dp);

            match self.transfer(&asset_info.transfer_code, transfer_amt, from, to, asset_info.dp).await {
                Ok(_) => println!("[REBALANCE] ✅ {} {:.prec$} {} → {}",
                    asset_info.transfer_code, transfer_amt, from_label, to_label, prec = asset_info.dp),
                Err(e) => eprintln!("[REBALANCE] ❌ Transfer failed for {}: {}", asset_info.transfer_code, e),
            }

            // Small delay between transfers
            tokio::time::sleep(tokio::time::Duration::from_millis(300)).await;
        }

        println!("[REBALANCE] Asset rebalance pass complete.");
    }

    /// Check and rebalance a specific asset pair before trading.
    /// Triggers immediate transfer if maker account has <20% of combined base asset
    /// or taker account has <20% of combined quote asset.
    /// Returns true if a rebalance was triggered.
    pub async fn rebalance_pair_if_needed(
        &self,
        base_currency: &str,
        quote_currency: &str,
        maker_account: &str,
        taker_account: &str,
        maker_is_selling: bool,
    ) -> bool {
        let mut rebalanced = false;
        let bal1 = match self.get_balances(self.cms1_id).await {
            Ok(b) => b,
            Err(e) => { eprintln!("[REBALANCE] Failed to fetch CMS1 balances: {}", e); return false; }
        };
        let bal2 = match self.get_balances(self.cms2_id).await {
            Ok(b) => b,
            Err(e) => { eprintln!("[REBALANCE] Failed to fetch CMS2 balances: {}", e); return false; }
        };

        // Determine which account needs which asset
        let (maker_id, taker_id) = if maker_account == "CMS1" {
            (self.cms1_id, self.cms2_id)
        } else {
            (self.cms2_id, self.cms1_id)
        };

        // If maker is selling, they need base asset. Taker needs quote asset.
        let (needed_by_maker, needed_by_taker) = if maker_is_selling {
            (base_currency, quote_currency)
        } else {
            // This shouldn't happen in our rotation, but handle it
            (quote_currency, base_currency)
        };

        let maker_balance = if maker_account == "CMS1" {
            bal1.get(needed_by_maker).copied().unwrap_or(0.0)
        } else {
            bal2.get(needed_by_maker).copied().unwrap_or(0.0)
        };

        let taker_balance = if taker_account == "CMS1" {
            bal1.get(needed_by_taker).copied().unwrap_or(0.0)
        } else {
            bal2.get(needed_by_taker).copied().unwrap_or(0.0)
        };

        let total_maker = maker_balance + if maker_account == "CMS1" {
            bal2.get(needed_by_maker).copied().unwrap_or(0.0)
        } else {
            bal1.get(needed_by_maker).copied().unwrap_or(0.0)
        };

        let total_taker = taker_balance + if taker_account == "CMS1" {
            bal2.get(needed_by_taker).copied().unwrap_or(0.0)
        } else {
            bal1.get(needed_by_taker).copied().unwrap_or(0.0)
        };

        // Check if maker has <20% of combined asset
        if total_maker > 0.0 && maker_balance / total_maker < 0.20 {
            let other_balance = total_maker - maker_balance;
            let transfer_amt = (other_balance - maker_balance) / 2.0;
            if transfer_amt > 0.001 {
                let (from, to, from_label, to_label) = if maker_account == "CMS1" {
                    (self.cms2_id, self.cms1_id, "CMS2", "CMS1")
                } else {
                    (self.cms1_id, self.cms2_id, "CMS1", "CMS2")
                };
                println!("[REBALANCE] ⚠️ {} low on {}: {:.4} vs {:.4} → transferring {:.4} from {} to {}",
                    maker_account, needed_by_maker, maker_balance, other_balance, transfer_amt, from_label, to_label);
                let _ = self.transfer(needed_by_maker, transfer_amt, from, to, 8).await;
                rebalanced = true;
            }
        }

        // Check if taker has <20% of combined quote asset
        if total_taker > 0.0 && taker_balance / total_taker < 0.20 {
            let other_balance = total_taker - taker_balance;
            let transfer_amt = (other_balance - taker_balance) / 2.0;
            if transfer_amt > 0.001 {
                let (from, to, from_label, to_label) = if taker_account == "CMS1" {
                    (self.cms2_id, self.cms1_id, "CMS2", "CMS1")
                } else {
                    (self.cms1_id, self.cms2_id, "CMS1", "CMS2")
                };
                println!("[REBALANCE] ⚠️ {} low on {}: {:.4} vs {:.4} → transferring {:.4} from {} to {}",
                    taker_account, needed_by_taker, taker_balance, other_balance, transfer_amt, from_label, to_label);
                let _ = self.transfer(needed_by_taker, transfer_amt, from, to, 8).await;
                rebalanced = true;
            }
        }
        
        rebalanced
    }

    /// Detect and correct systemic imbalance where BOTH accounts have excess base asset.
    /// 
    /// ⚠️ DISABLED — This function was firing every cycle and placing market SELL orders
    /// against external liquidity, causing rapid capital bleed (~1% spread loss per 15s).
    /// The wash-trading bot naturally holds base assets; a 60% base threshold is normal
    /// and does not require corrective market sells. Use internal transfers only.
    /// 
    /// Returns false always (disabled).
    pub async fn correct_base_asset_surplus(
        &self,
        _base_currency: &str,
        _quote_currency: &str,
        _pair_symbol: &str,
        _current_price: f64,
    ) -> bool {
        // DISABLED: was causing market sells against external liquidity every cycle.
        // The rebalancer's background 90s interval handles internal transfers safely.
        false
    }

    /// Place a market sell order via REST API
    async fn place_market_sell(&self, pair: &str, qty: f64, subaccount: u64) -> Result<()> {
        use hmac::{Hmac, Mac};
        use sha2::Sha512;

        let path = "/v1/orders";
        let body = serde_json::json!({
            "currencyPair": pair,
            "side": "SELL",
            "type": "MARKET",
            "quantity": format!("{:.8}", qty)
        });
        let body_str = serde_json::to_string(&body)?;

        let ts = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64;
        let sub_str = if subaccount == 0 { String::new() } else { subaccount.to_string() };
        let msg = format!("{}{}{}{}{}", ts, "POST", path, body_str, sub_str);

        let mut mac = Hmac::<Sha512>::new_from_slice(self.api_secret.as_bytes()).unwrap();
        mac.update(msg.as_bytes());
        let sig = hex::encode(mac.finalize().into_bytes());

        let client = reqwest::Client::new();
        let resp = client
            .post(format!("https://api.valr.com{}", path))
            .header("X-VALR-API-KEY", &self.api_key)
            .header("X-VALR-SIGNATURE", sig)
            .header("X-VALR-TIMESTAMP", ts.to_string())
            .header("X-VALR-SUB-ACCOUNT-ID", subaccount.to_string())
            .header("Content-Type", "application/json")
            .body(body_str)
            .send()
            .await?;

        let status = resp.status();
        if status.is_success() || status.as_u16() == 202 {
            Ok(())
        } else {
            let text = resp.text().await.unwrap_or_default();
            anyhow::bail!("Market sell failed ({}): {}", status, text)
        }
    }
}
