use crate::pair_info::PairInfo;
use crate::price_feed::OrderbookPrices;
use crate::types::OrderSide;
use crate::ws_client::WsClient;
use rand::Rng;

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
    let mut rng = rand::thread_rng();
    let multiplier = rng.gen_range(qty_range_min..=qty_range_max);

    let qty = calculate_order_qty(
        pair_info.min_qty,
        pair_info.min_value,
        prices.mid,
        pair_info.qty_precision,
        multiplier,
    );

    // Use mid price from WS orderbook.
    // If spread > 50bps, fall back to mark price — wide spreads mean mid is unreliable
    // and postOnlyReprice may reprice far from where the taker can self-match.
    let spread_bps = if prices.mid > 0.0 {
        ((prices.ask - prices.bid) / prices.mid) * 10_000.0
    } else {
        0.0
    };

    let base_price = if spread_bps > 50.0 {
        if let Some(mark) = prices.mark_price {
            println!("[INFO] Spread {:.1}bps > 50bps for {} — using mark price {}", spread_bps, pair_info.symbol, mark);
            mark
        } else {
            println!("[INFO] Spread {:.1}bps > 50bps for {} — no mark price available, using mid", spread_bps, pair_info.symbol);
            prices.mid
        }
    } else {
        prices.mid
    };

    let maker_price = round_price(base_price, pair_info.price_precision);
    let taker_price = maker_price;

    println!("[INFO] Cycle: {} {:?} @ {} vs {} {:?} @ {} | Qty: {}",
        maker_account, maker_side, maker_price,
        taker_account, maker_side.opposite(), taker_price, qty);

    let maker_side_str = maker_side.to_string();
    let taker_side_str = maker_side.opposite().to_string();

    // Step 1: Place maker via WS and await ORDER_PLACED confirmation.
    // Use plain postOnly (no reprice) to ensure maker rests at exact price.
    // postOnlyReprice would reprice the order away from our calculated price,
    // causing the taker to miss it and fill externally.
    let maker_order_id = match maker_ws.place_order(
        &pair_info.symbol,
        &maker_side_str,
        qty,
        maker_price,
        true,  // post_only
        "GTC",
    ).await {
        Ok(id) => {
            println!("[INFO] {} Maker placed: {} {} @ {} → {}", maker_account, maker_side, qty, maker_price, &id[..8]);
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

    // Step 2: Maker is confirmed on book — immediately send taker IOC via WS.
    let taker_order_id = match taker_ws.place_order(
        &pair_info.symbol,
        &taker_side_str,
        qty,
        taker_price,
        false, // not post_only
        "IOC",
    ).await {
        Ok(id) => {
            println!("[INFO] {} Taker placed: {} {} @ {} → {}", taker_account, maker_side.opposite(), qty, taker_price, &id[..8]);
            Some(id)
        }
        Err(e) => {
            eprintln!("[ERROR] {} Taker failed: {}", taker_account, e);
            // Cancel the resting maker to avoid orphaned GTC
            for attempt in 1..=3 {
                // Use REST for cancel since we don't have WS cancel wired up yet
                // (cancel is rare — only on taker failure)
                eprintln!("[WARN] Taker failed — maker {} left resting (attempt {} cancel not implemented via WS, leaving for cleanup)",
                    &maker_order_id[..8], attempt);
                break;
            }
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
