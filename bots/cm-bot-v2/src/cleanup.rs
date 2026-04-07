use crate::valr::ValrClient;
use chrono::{Duration, Utc};

pub struct CleanupResult {
    pub cancelled_count: usize,
    pub errors: Vec<String>,
}

/// Periodic cleanup: cancel orders older than age_threshold_ms across all symbols.
/// Runs every cleanup_interval_ms to sweep any orphaned GTC makers.
pub async fn cleanup_stale_orders(
    client: &ValrClient,
    symbols: &[&str],
    age_threshold_ms: u64,
) -> CleanupResult {
    let mut cancelled_count = 0;
    let mut errors = Vec::new();

    println!("[INFO] Starting cleanup task (threshold: {}ms)", age_threshold_ms);

    // VALR ignores the ?pair= filter for futures — fetch all open orders once and
    // filter client-side by currencyPair to avoid redundant API calls.
    let all_orders = match client.get_open_orders(None).await {
        Ok(orders) => orders,
        Err(e) => {
            eprintln!("[ERROR] Failed to get open orders: {}", e);
            errors.push(format!("Failed to get open orders: {}", e));
            return CleanupResult { cancelled_count, errors };
        }
    };

    let now = Utc::now();
    let threshold = Duration::milliseconds(age_threshold_ms as i64);

    for order in all_orders {
        // Only process orders for our tracked symbols
        if !symbols.contains(&order.symbol.as_str()) {
            continue;
        }
        let age = now.signed_duration_since(order.created_at);
        if age > threshold {
            match client.cancel_order(&order.symbol, &order.id).await {
                Ok(_) => {
                    println!("[INFO] Cancelled stale order {} for {} (age: {}ms)",
                        &order.id[..8], order.symbol, age.num_milliseconds());
                    cancelled_count += 1;
                }
                Err(e) => {
                    eprintln!("[ERROR] Failed to cancel order {}: {}", &order.id[..8], e);
                    errors.push(format!("Failed to cancel {}: {}", order.id, e));
                }
            }
            tokio::time::sleep(tokio::time::Duration::from_millis(100)).await;
        }
    }

    println!("[INFO] Cleanup complete: {} orders cancelled", cancelled_count);

    CleanupResult { cancelled_count, errors }
}

/// Pre-cycle cleanup: cancel ALL open orders for a single symbol on a single account.
/// Called before each cycle to ensure no stale GTC makers are resting from a previous
/// failed taker (prevents -11500 STP errors and "Insufficient Balance" from locked margin).
pub async fn cancel_all_open_orders_for_symbol(
    client: &ValrClient,
    account: &str,
    symbol: &str,
) -> usize {
    // Fetch all orders (VALR ignores ?pair= filter for futures) and filter client-side.
    match client.get_open_orders(None).await {
        Ok(orders) => {
            let matching: Vec<_> = orders.into_iter().filter(|o| o.symbol == symbol).collect();
            if matching.is_empty() {
                return 0;
            }
            println!("[INFO] Cancelling {} open orders on {} for {}",
                matching.len(), account, symbol);
            let mut cancelled = 0;
            for order in &matching {
                match client.cancel_order(&order.symbol, &order.id).await {
                    Ok(_) => cancelled += 1,
                    Err(e) => eprintln!("[WARN] Cancel failed for {} {}: {}",
                        account, &order.id[..8], e),
                }
            }
            cancelled
        }
        Err(e) => {
            eprintln!("[WARN] get_open_orders failed for {} {}: {}", account, symbol, e);
            0
        }
    }
}
