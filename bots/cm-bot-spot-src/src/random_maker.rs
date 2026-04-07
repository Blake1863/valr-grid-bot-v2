/// Randomized maker selection for organic-looking trade patterns.
/// 
/// Uses Option B: Random with balance tracking
/// - Tracks last 10 cycles' maker selection
/// - Biases toward underrepresented account
/// - Hard cap: max 5 consecutive same-side cycles

use rand::Rng;

pub struct RandomMakerSelector {
    history: Vec<bool>,  // true = CM1, false = CM2
    consecutive_same: u32,
    last_maker: Option<bool>,
}

impl RandomMakerSelector {
    pub fn new() -> Self {
        Self {
            history: Vec::with_capacity(10),
            consecutive_same: 0,
            last_maker: None,
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
    
    /// Also randomly pick maker side (buy or sell) for organic taker direction.
    /// Returns true for Sell (taker buys), false for Buy (taker sells).
    pub fn select_maker_side(&self) -> bool {
        let mut rng = rand::thread_rng();
        rng.gen_bool(0.5)
    }

    /// Get current stats for debugging
    pub fn get_stats(&self) -> (usize, usize, u32) {
        let cms1 = self.history.iter().filter(|&&x| x).count();
        let cms2 = self.history.len() - cms1;
        (cms1, cms2, self.consecutive_same)
    }
}

impl Default for RandomMakerSelector {
    fn default() -> Self {
        Self::new()
    }
}
