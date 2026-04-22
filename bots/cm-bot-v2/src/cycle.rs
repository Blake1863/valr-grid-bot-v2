use crate::pair_info::PairInfo;
use crate::price_feed::OrderbookPrices;
use crate::types::OrderSide;
use crate::ws_client::WsClient;
use rand::Rng;
use std::time::{Duration, Instant};

/// Max age for the orderbook snapshot before we skip a cycle.
/// Our OB_L1_DIFF feed pushes on every tick — anything older than this
/// probably means the WS is delayed / hiccuping.
const MAX_ORDERBOOK_AGE: Duration = Duration::from_millis(500);

/// If the book was updated within this window before we fire, the price is
/// jittery — skip this cycle so we don't race against our own update.
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
        return CycleResult {
            success: false,
            maker_order_id: None,
            taker_order_id: None,
            external_fill: false,
            error: Some(format!(
                "skip: stale orderbook for {} ({}ms old)",
                pair_info.symbol, age.as_millis()
            )),
        };
    }

    // ── Jitter check (Fix E) ──────────────────────────────────────────────
    // If the book ticked very recently, give it a beat — otherwise our maker
    // may land right as another tick flips best-bid/ask.
    if let Some(prev) = prices.prev_updated_at {
        let since_prev = prices.updated_at.duration_since(prev);
        if since_prev < MIN_STABLE_WINDOW {
            return CycleResult {
                success: false,
                maker_order_id: None,
                taker_order_id: None,
                external_fill: false,
                error: Some(format!(
                    "skip: book jittering for {} (tick gap {}ms)",
                    pair_info.symbol, since_prev.as_millis()
                )),
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

    // Spread guard: VALR perps frequently have 5–25bps spreads, so the old
    // 50bps threshold effectively never triggered the mark-price fallback.
    // Drop to 25bps so wide-spread pairs fall back safely.
    let spread_bps = if prices.mid > 0.0 {
        ((prices.ask - prices.bid) / prices.mid) * 10_000.0
    } else {
        0.0
    };

    let base_price = if spread_bps > 25.0 {
        if let Some(mark) = prices.mark_price {
            println!("[INFO] Spread {:.1}bps > 25bps for {} — using mark price {}",
                spread_bps, pair_info.symbol, mark);
            mark
        } else {
            prices.mid
        }
    } else {
        prices.mid
    };

    // Place the maker *inside* the spread so that (a) post-only is guaranteed
    // to rest and (b) OUR order is the only one at that price level — giving
    // our own taker a fresh, private price to match against.
    //
    // We prefer the one-tick-inside position; if the spread is only 1 tick
    // (no room inside), fall back to resting at the best bid/ask. That case
    // is inherently racy — another bot on the book can fill ahead of us.
    let tick = 10_f64.powi(-(pair_info.price_precision as i32));
    let _ = base_price; // base_price currently unused for placement, but kept for logging

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
    let taker_price = maker_price;

    // Sanity: if after adjustment the maker_price crosses, skip.
    if (maker_side == OrderSide::Buy && maker_price >= prices.ask)
        || (maker_side == OrderSide::Sell && maker_price <= prices.bid)
    {
        return CycleResult {
            success: false,
            maker_order_id: None,
            taker_order_id: None,
            external_fill: false,
            error: Some(format!(
                "skip: no safe maker price for {} (bid={} ask={} maker={})",
                pair_info.symbol, prices.bid, prices.ask, maker_price
            )),
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
