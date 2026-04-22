use crate::pair_info::PairInfo;
use crate::price_feed::OrderbookPrices;
use crate::types::OrderSide;
use crate::ws_client::WsClient;
use rand::Rng;
use std::time::{Duration, Instant};

/// Max age for the orderbook snapshot before we skip a cycle.
/// AGGREGATED_ORDERBOOK_UPDATE cadence is ~100–500ms on active pairs; anything
/// older than 2s likely means the WS is wedged.
const MAX_ORDERBOOK_AGE: Duration = Duration::from_secs(2);

/// If the book ticked within this window, the price is jittery — skip so we
/// don't race against our own tick.
const MIN_STABLE_WINDOW: Duration = Duration::from_millis(20);

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

    // ── Jitter check (Fix E) ──────────────────────────────────────────────
    if let Some(prev) = prices.prev_updated_at {
        let since_prev = prices.updated_at.duration_since(prev);
        if since_prev < MIN_STABLE_WINDOW {
            let reason = format!(
                "skip: book jittering for {} (tick gap {}ms)",
                pair_info.symbol, since_prev.as_millis()
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

    // Step 1: Place maker via WS and wait for ORDER_STATUS_UPDATE confirming
    // the order actually rested on the book (Fix C — place_maker resolves
    // on status update, not on the bare ACK).
    let t_maker = Instant::now();
    let maker_order_id = match maker_ws.place_maker(
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
        external_fill: false,
        error: None,
    }
}
