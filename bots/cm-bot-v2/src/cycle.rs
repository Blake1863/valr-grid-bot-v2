use crate::pair_info::PairInfo;
use crate::price_feed::OrderbookPrices;
use crate::types::OrderSide;
use crate::ws_client::WsClient;
use rand::Rng;
use std::time::{Duration, Instant};

/// Max age for the orderbook snapshot before we skip a cycle.
/// AGGREGATED_ORDERBOOK_UPDATE cadence is ~100–500ms on active pairs, but can
/// exceed 10s on thin perps when top-of-book doesn't change. 15s is a safe
/// ceiling — anything older likely means the WS is wedged.
const MAX_ORDERBOOK_AGE: Duration = Duration::from_secs(15);

// Jitter guard removed: inside-spread maker pricing + fresh book snapshot
// already protect against tick races. Over-filtering was losing ~60% of
// cycles to false positives.

pub fn calculate_order_qty(
    min_qty: f64,
    min_value: f64,
    mark_price: f64,
    qty_precision: u8,
    multiplier: f64,
) -> f64 {
    let precision_factor = 10_f64.powi(qty_precision as i32);
    let min_value_qty = min_value / mark_price;
    let min_value_qty_rounded = (min_value_qty * precision_factor).ceil() / precision_factor;
    let effective_min = min_qty.max(min_value_qty_rounded);
    let raw_qty = effective_min * multiplier;
    (raw_qty * precision_factor).ceil() / precision_factor
}

pub fn round_price(price: f64, precision: u8) -> f64 {
    let factor = 10_f64.powi(precision as i32);
    (price * factor).round() / factor
}

pub struct CycleResult {
    pub success: bool,
    pub maker_order_id: Option<String>,
    pub taker_order_id: Option<String>,
    pub external_fill: bool,
    pub error: Option<String>,
}

/// Verify that both maker and taker filled against each other (not external).
/// Waits up to 3s for NEW_ACCOUNT_TRADE events on both sides.
async fn verify_internal_fill(
    maker_ws: &crate::ws_client::WsClient,
    taker_ws: &crate::ws_client::WsClient,
    expected_price: f64,
    expected_qty: f64,
) -> bool {
    let timeout = tokio::time::Duration::from_secs(3);
    let tolerance_bps = 5; // 0.05% price tolerance for rounding

    let maker_start = Instant::now();
    let taker_start = Instant::now();

    let mut maker_trade = None;
    let mut taker_trade = None;

    // Poll both sides for their NEW_ACCOUNT_TRADE events
    while maker_trade.is_none() || taker_trade.is_none() {
        let elapsed = maker_start.elapsed();
        if elapsed > timeout {
            break;
        }

        if maker_trade.is_none() {
            if let Some(trade) = maker_ws.pop_latest_trade().await {
                maker_trade = Some(trade);
            }
        }
        if taker_trade.is_none() {
            if let Some(trade) = taker_ws.pop_latest_trade().await {
                taker_trade = Some(trade);
            }
        }

        if maker_trade.is_none() || taker_trade.is_none() {
            tokio::time::sleep(tokio::time::Duration::from_millis(10)).await;
        }
    }

    // Analyze results
    match (maker_trade, taker_trade) {
        (Some(mt), Some(tt)) => {
            let dt_ms = (mt.timestamp_ms as i64 - tt.timestamp_ms as i64).abs();
            let price_diff_bps = if expected_price > 0.0 {
                ((mt.price - tt.price).abs() / expected_price) * 10_000.0
            } else {
                999.0
            };
            let qty_match = (mt.quantity - expected_qty).abs() < expected_qty * 0.01; // 1% tolerance
            let price_match = price_diff_bps < tolerance_bps as f64;

            if price_match && qty_match && dt_ms < 500 {
                println!("[INFO] ✅ Internal fill verified: {} @ {} x {} (Δ={}ms)",
                    mt.pair, mt.price, mt.quantity, dt_ms);
                true
            } else {
                eprintln!("[WARN] 🚨 MISMATCH DETECTED on {} | maker: {} @ {} x {} | taker: {} @ {} x {} | Δ={}ms | price_diff={}bps",
                    mt.pair,
                    mt.side, mt.price, mt.quantity,
                    tt.side, tt.price, tt.quantity,
                    dt_ms, price_diff_bps);
                false
            }
        }
        (Some(mt), None) => {
            eprintln!("[WARN] 🚨 EXTERNAL FILL: maker {} filled ({} @ {} x {}) but taker {} has no matching trade within {}ms",
                mt.pair, mt.side, mt.price, mt.quantity,
                taker_ws.account_name,
                maker_start.elapsed().as_millis());
            false
        }
        (None, Some(tt)) => {
            eprintln!("[WARN] 🚨 EXTERNAL FILL: taker {} filled ({} @ {} x {}) but maker {} has no matching trade within {}ms",
                tt.pair, tt.side, tt.price, tt.quantity,
                maker_ws.account_name,
                taker_start.elapsed().as_millis());
            false
        }
        (None, None) => {
            eprintln!("[WARN] 🚨 NO TRADES on either side within {}ms — orders may still be resting",
                timeout.as_millis());
            false
        }
    }
}

pub async fn execute_cycle(
    maker_ws: &WsClient,
    taker_ws: &WsClient,
    pair_info: &PairInfo,
    prices: &OrderbookPrices,
    maker_account: &str,
    taker_account: &str,
    maker_side: OrderSide,
) -> CycleResult {
    execute_cycle_with_qty_range(
        maker_ws, taker_ws, pair_info, prices,
        maker_account, taker_account, maker_side,
        1.0, 1.5,
    ).await
}

pub async fn execute_cycle_with_qty_range(
    maker_ws: &WsClient,
    taker_ws: &WsClient,
    pair_info: &PairInfo,
    prices: &OrderbookPrices,
    maker_account: &str,
    taker_account: &str,
    maker_side: OrderSide,
    qty_range_min: f64,
    qty_range_max: f64,
) -> CycleResult {
    // ── Freshness check (Fix B) ───────────────────────────────────────────
    let age = prices.updated_at.elapsed();
    if age > MAX_ORDERBOOK_AGE {
        let reason = format!(
            "skip: stale orderbook for {} ({}ms old)",
            pair_info.symbol, age.as_millis()
        );
        eprintln!("[WARN] {}", reason);
        return CycleResult {
            success: false,
            maker_order_id: None,
            taker_order_id: None,
            external_fill: false,
            error: Some(reason),
        };
    }

    let mut rng = rand::thread_rng();
    let multiplier = rng.gen_range(qty_range_min..=qty_range_max);

    let qty = calculate_order_qty(
        pair_info.min_qty,
        pair_info.min_value,
        prices.mid,
        pair_info.qty_precision,
        multiplier,
    );

    // Always price the maker one tick INSIDE the current spread so:
    //   (a) post-only is guaranteed to rest (never crosses),
    //   (b) our order is the ONLY order at that price,
    //   (c) our taker can match exclusively against our maker (no external
    //       liquidity lurks between best-bid/ask and our maker).
    //
    // If the spread is only 1 tick wide (no room inside), we fall back to
    // resting at the best bid/ask — this is inherently racy (another bot on
    // the book may be first) so we log and accept the risk. We explicitly do
    // NOT fall back to mark price, because that would place us far from the
    // true best bid/ask and let the taker route through external liquidity
    // before hitting our maker.
    let tick = 10_f64.powi(-(pair_info.price_precision as i32));
    let spread_bps = if prices.mid > 0.0 {
        ((prices.ask - prices.bid) / prices.mid) * 10_000.0
    } else {
        0.0
    };

    let maker_price = match maker_side {
        OrderSide::Buy => {
            let inside = prices.bid + tick;
            let p = if inside < prices.ask { inside } else { prices.bid };
            round_price(p, pair_info.price_precision)
        }
        OrderSide::Sell => {
            let inside = prices.ask - tick;
            let p = if inside > prices.bid { inside } else { prices.ask };
            round_price(p, pair_info.price_precision)
        }
    };
    // Taker price crosses through our maker. For a buy-taker, price = maker_price
    // (SELL at that price or lower will match). For a sell-taker, same.
    // Using the SAME price as maker guarantees the IOC will only match against
    // levels at or better than our maker — which in a 1-tick-inside setup is
    // ONLY our maker.
    let taker_price = maker_price;

    // Sanity: if after adjustment the maker_price crosses, skip.
    if (maker_side == OrderSide::Buy && maker_price >= prices.ask)
        || (maker_side == OrderSide::Sell && maker_price <= prices.bid)
    {
        let reason = format!(
            "skip: no safe maker price for {} (bid={} ask={} maker={})",
            pair_info.symbol, prices.bid, prices.ask, maker_price
        );
        eprintln!("[WARN] {}", reason);
        return CycleResult {
            success: false,
            maker_order_id: None,
            taker_order_id: None,
            external_fill: false,
            error: Some(reason),
        };
    }

    println!("[INFO] Cycle: {} {:?} @ {} vs {} {:?} @ {} | Qty: {} | book age={}ms bid={} ask={}",
        maker_account, maker_side, maker_price,
        taker_account, maker_side.opposite(), taker_price, qty,
        age.as_millis(), prices.bid, prices.ask);

    let maker_side_str = maker_side.to_string();
    let taker_side_str = maker_side.opposite().to_string();

    // Step 1: Place maker via WS and resolve immediately on ACK (no grace timer).
    // This minimizes the window for external fills — taker follows in ~5ms.
    let t_maker = Instant::now();
    let maker_order_id = match maker_ws.place_maker_fast(
        &pair_info.symbol,
        &maker_side_str,
        qty,
        maker_price,
    ).await {
        Ok(id) => {
            println!("[INFO] {} Maker placed: {} {} @ {} → {} ({}ms)",
                maker_account, maker_side, qty, maker_price,
                &id[..8.min(id.len())], t_maker.elapsed().as_millis());
            id
        }
        Err(e) => {
            eprintln!("[ERROR] {} Maker failed: {}", maker_account, e);
            return CycleResult {
                success: false,
                maker_order_id: None,
                taker_order_id: None,
                external_fill: false,
                error: Some(format!("Maker failed: {}", e)),
            };
        }
    };

    // Step 2: Maker is confirmed on book — send taker IOC.
    let t_taker = Instant::now();
    let taker_order_id = match taker_ws.place_taker(
        &pair_info.symbol,
        &taker_side_str,
        qty,
        taker_price,
    ).await {
        Ok(id) => {
            println!("[INFO] {} Taker placed: {} {} @ {} → {} ({}ms)",
                taker_account, maker_side.opposite(), qty, taker_price,
                &id[..8.min(id.len())], t_taker.elapsed().as_millis());
            Some(id)
        }
        Err(e) => {
            eprintln!("[ERROR] {} Taker failed: {}", taker_account, e);
            eprintln!("[WARN] Taker failed — maker {} left resting for cleanup",
                &maker_order_id[..8.min(maker_order_id.len())]);
            return CycleResult {
                success: false,
                maker_order_id: Some(maker_order_id),
                taker_order_id: None,
                external_fill: false,
                error: Some(format!("Taker failed: {}", e)),
            };
        }
    };

    CycleResult {
        success: true,
        maker_order_id: Some(maker_order_id),
        taker_order_id,
        external_fill: !verify_internal_fill(
            maker_ws,
            taker_ws,
            maker_price,
            qty,
        ).await,
        error: None,
    }
}
