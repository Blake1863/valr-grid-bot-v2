/// Randomized maker selection for organic-looking trade patterns.
/// 
/// Uses Option B: Random with balance tracking
/// - Tracks last 10 cycles' maker selection
/// - Biases toward underrepresented account
/// - Hard cap: max 5 consecutive same-side cycles
/// - Position-aware: tracks net position delta per pair (CM1 − CM2) and
///   FORCES corrective side when delta exceeds threshold (no random escape)

use rand::Rng;
use std::collections::HashMap;

/// Delta beyond which we FORCE the corrective side (no random chance).
/// Below this, we still bias but allow randomness.
const POSITION_FORCE_THRESHOLD: f64 = 5.0;

/// Maximum absolute delta we'll store (safety clamp).
const POSITION_CLAMP: f64 = 500.0;

pub struct RandomMakerSelector {
    history: Vec<bool>,  // true = CM1, false = CM2
    consecutive_same: u32,
    last_maker: Option<bool>,
    /// Per-pair position delta: CM1_qty − CM2_qty (in position units).
    /// Positive = CM1 is net long relative to CM2.
    /// Negative = CM2 is net long relative to CM1.
    position_delta: HashMap<String, f64>,
}

impl RandomMakerSelector {
    pub fn new() -> Self {
        Self {
            history: Vec::with_capacity(10),
            consecutive_same: 0,
            last_maker: None,
            position_delta: HashMap::new(),
        }
    }
    
    /// Select maker account randomly with balance bias.
    /// Returns true for CM1, false for CM2.
    pub fn select_maker(&mut self) -> bool {
        let mut rng = rand::thread_rng();
        
        // Calculate bias based on last 10 cycles
        let cms2_probability = if self.history.len() >= 10 {
            let cms1_count = self.history.iter().filter(|&&x| x).count();
            let cms2_count = self.history.len() - cms1_count;
            
            if cms1_count > cms2_count {
                // CM1 sold more, bias toward CM2 (up to 70%)
                0.5 + (cms1_count - cms2_count) as f64 / 20.0
            } else if cms2_count > cms1_count {
                // CM2 sold more, bias toward CM1 (down to 30%)
                0.5 - (cms2_count - cms1_count) as f64 / 20.0
            } else {
                0.5  // Balanced, 50/50
            }
        } else {
            0.5  // Not enough history, 50/50
        };
        
        // Check consecutive cap (max 5 same side in a row)
        let maker = if self.consecutive_same >= 5 {
            // Force flip
            !self.last_maker.unwrap_or(true)
        } else {
            // Random selection with bias (cms2_probability is for CM2, so invert for CM1)
            let is_cm1 = rng.gen_bool(1.0 - cms2_probability);
            
            // Update consecutive counter
            if Some(is_cm1) == self.last_maker {
                self.consecutive_same += 1;
            } else {
                self.consecutive_same = 1;
            }
            
            is_cm1
        };
        
        // Update history
        self.history.push(maker);
        if self.history.len() > 10 {
            self.history.remove(0);
        }
        self.last_maker = Some(maker);
        
        maker
    }
    
    /// Choose maker side (Buy or Sell) with position correction.
    ///
    /// When |position_delta| >= POSITION_FORCE_THRESHOLD, the corrective side
    /// is FORCED (no random escape). This prevents both accounts from
    /// accumulating in the same direction.
    ///
    /// Below the threshold, we bias toward correction but allow randomness.
    ///
    /// Does NOT update the delta — call record_cycle_result() after the cycle
    /// completes successfully to commit the position change.
    ///
    /// Returns true for Sell (maker sells, taker buys), false for Buy.
    pub fn select_maker_side(&mut self, is_cm1_maker: bool, symbol: &str) -> bool {
        let mut rng = rand::thread_rng();

        let delta = self.position_delta.entry(symbol.to_string()).or_insert(0.0);

        // Check if we need to FORCE the corrective side.
        // When forcing, the corrective side depends on WHICH account is maker:
        //   delta > 0 (CM1 too long) → reduce CM1 long OR increase CM2 long
        //     - CM1 maker → CM1 SELLs  (true)
        //     - CM2 maker → CM2 BUYs   (false) — CM2 buys → CM2 less short
        //   delta < 0 (CM2 too long) → reduce CM2 long OR increase CM1 long
        //     - CM1 maker → CM1 BUYs   (false)
        //     - CM2 maker → CM2 SELLs  (true)
        // This is just `is_cm1_maker` for delta > 0, `!is_cm1_maker` for delta < 0.
        if *delta >= POSITION_FORCE_THRESHOLD {
            return is_cm1_maker;  // CM1 sells, or CM2 buys
        }
        if *delta <= -POSITION_FORCE_THRESHOLD {
            return !is_cm1_maker;  // CM1 buys, or CM2 sells
        }

        // Below threshold: bias toward correction but allow randomness.
        // Compute the probability that the MAKER should SELL.
        // When delta > 0 (CM1 too long), corrective action is:
        //   CM1 SELL or CM2 BUY → so sell_prob > 0.5 when CM1 is maker,
        //   but sell_prob < 0.5 when CM2 is maker (CM2 should buy).
        // When delta < 0 (CM2 too long), corrective action is:
        //   CM1 BUY or CM2 SELL → so sell_prob < 0.5 when CM1 is maker,
        //   but sell_prob > 0.5 when CM2 is maker (CM2 should sell).
        let correction_bias = if *delta > 0.0 {
            // CM1 long → want maker to SELL if CM1, BUY if CM2
            (0.5 + (*delta * 0.05).min(0.3)).min(0.8)
        } else if *delta < 0.0 {
            // CM2 long → want maker to BUY if CM1, SELL if CM2
            (0.5 + (*delta * 0.05)).max(0.2)
        } else {
            0.5
        };
        let sell_prob = if is_cm1_maker {
            correction_bias
        } else {
            1.0 - correction_bias  // flip: CM2 selling is opposite of CM1 selling for delta
        };

        rng.gen_bool(sell_prob)
    }

    /// Commit a completed cycle's position impact to the delta counter.
    /// Must only be called when the cycle actually succeeded (both maker
    /// and taker confirmed filled).
    pub fn record_cycle_result(&mut self, is_cm1_maker: bool, maker_sells: bool, symbol: &str) {
        let delta = self.position_delta.entry(symbol.to_string()).or_insert(0.0);
        let d = match (is_cm1_maker, maker_sells) {
            (true, false) => 1.0,   // CM1 buys → net long
            (true, true) => -1.0,   // CM1 sells → net short
            (false, false) => -1.0, // CM2 buys (CM1 net short)
            (false, true) => 1.0,   // CM2 sells (CM1 net long)
        };
        *delta += d;
        *delta = (*delta).clamp(-POSITION_CLAMP, POSITION_CLAMP);
    }

    /// Reconcile internal position delta with actual exchange positions.
    /// Called periodically (every ~30 cycles) via REST /v1/positions/open.
    ///
    /// `delta` = CM1 quantity − CM2 quantity for this pair.
    pub fn reconcile_delta(&mut self, symbol: &str, delta: f64) {
        let old = self.position_delta.get(symbol).copied().unwrap_or(0.0);
        let forced = delta.abs() >= POSITION_FORCE_THRESHOLD;

        if forced {
            println!("[RECONCILE] ⚠️ {} delta={:.4} — FORCING corrective side", symbol, delta);
        } else if delta.abs() > 0.01 {
            println!("[RECONCILE] {} delta={:.4} — biasing (no force)", symbol, delta);
        } else if old.abs() > 0.01 {
            println!("[RECONCILE] {} delta={:.4} — cleared (was {:.4})", symbol, delta, old);
        }

        self.position_delta.insert(symbol.to_string(), delta.clamp(-POSITION_CLAMP, POSITION_CLAMP));
    }

    /// Get current stats for debugging
    pub fn get_stats(&self) -> (usize, usize, u32) {
        let cms1 = self.history.iter().filter(|&&x| x).count();
        let cms2 = self.history.len() - cms1;
        (cms1, cms2, self.consecutive_same)
    }
    
    /// Get position delta for a symbol (for debugging)
    pub fn get_position_delta(&self, symbol: &str) -> f64 {
        *self.position_delta.get(symbol).unwrap_or(&0.0)
    }
}

impl Default for RandomMakerSelector {
    fn default() -> Self {
        Self::new()
    }
}
