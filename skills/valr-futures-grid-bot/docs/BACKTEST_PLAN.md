# Backtesting Framework — VALR Futures Grid Bot

## Overview

Systematic backtesting to optimize grid parameters for risk-adjusted returns.

## Parameter Constraints

### Grid Spacing: 0.1% – 3.0%

| Spacing | Use Case | Characteristics |
|---|---|---|
| 0.1–0.5% | Scalping | High frequency, tight ranges, low per-trade profit |
| 0.5–1.5% | Standard | Balanced frequency/profit, most market conditions |
| 1.5–3.0% | Swing | Low frequency, wide ranges, high per-trade profit |

**Rationale**: 
- <0.1%: Too tight, fees eat profits, excessive whipsaw
- >3%: Too wide, few fills, capital inefficiency

### Stop-Loss Distance: Must Exceed Grid Range

```
stop_loss_pct > (grid_levels × spacing_pct)
```

**Example** (3 levels, 0.5% spacing):
- Grid range: 3 × 0.5% = 1.5% (±1.5% from mid)
- Minimum SL: >1.5% (e.g., 2.5–3.0%)
- **Why**: Avoid stopping out from normal grid oscillation

**Valid Combinations**:

| Levels | Spacing | Grid Range | Min SL | Recommended SL |
|---|---|---|---|---|
| 3 | 0.25% | 0.75% | >0.75% | 2.0–3.0% |
| 3 | 0.5% | 1.5% | >1.5% | 2.5–4.0% |
| 5 | 0.3% | 1.5% | >1.5% | 2.5–4.0% |
| 5 | 1.0% | 5.0% | >5.0% | 6.0–8.0% |
| 10 | 0.15% | 1.5% | >1.5% | 2.5–4.0% |

## Optimization Metrics

### Primary Metrics

1. **Sharpe Ratio** (risk-adjusted returns)
   ```
   Sharpe = (Annualized Return - Risk Free Rate) / Annualized Std Dev
   ```
   - Target: >1.5 (good), >2.0 (excellent)
   - Penalizes volatile equity curves

2. **Total Net Profit** (absolute returns)
   - Sum of: Grid PnL + Funding + Realized PnL
   - Target: Positive across market conditions

3. **Maximum Drawdown** (worst peak-to-trough)
   - Target: <20% (conservative), <35% (aggressive)
   - Lower = better capital preservation

### Secondary Metrics

- **Win Rate**: % profitable trades
- **Profit Factor**: Gross Profit / Gross Loss (target >1.5)
- **Avg Trade Duration**: Minutes per grid cycle
- **Capital Efficiency**: Profit / (Avg Capital Deployed)
- **Sharpe-Adjusted Profit**: Profit × Sharpe (combined metric)

## Risk Profiles

Users select based on risk appetite:

### Conservative (Capital Preservation)
```json
{
  "leverage": 2–3,
  "levels": 5–10,
  "spacing_pct": 0.2–0.5,
  "max_loss_pct": 2.0–3.0,
  "balance_usage_pct": 50–70
}
```
- **Goal**: Steady returns, low drawdown
- **Best for**: Ranging markets, low volatility
- **Expected**: Sharpe >2.0, DD <15%, lower absolute profit

### Balanced (Risk-Adjusted Growth)
```json
{
  "leverage": 5,
  "levels": 3–5,
  "spacing_pct": 0.5–1.0,
  "max_loss_pct": 3.0–5.0,
  "balance_usage_pct": 80–90
}
```
- **Goal**: Best Sharpe, moderate drawdown
- **Best for**: Most market conditions
- **Expected**: Sharpe 1.5–2.0, DD 15–25%

### Aggressive (Max Profit)
```json
{
  "leverage": 10,
  "levels": 2–3,
  "spacing_pct": 1.0–2.0,
  "max_loss_pct": 5.0–8.0,
  "balance_usage_pct": 90–95
}
```
- **Goal**: Maximum absolute profit
- **Best for**: High volatility, strong conviction
- **Expected**: Sharpe 1.0–1.5, DD 25–40%

## Backtest Methodology

### Data Requirements

1. **OHLCV Data**: 1-minute or 5-minute candles
2. **Funding Rates**: Hourly snapshots
3. **Mark Price**: For accurate PnL calculation
4. **Period**: Minimum 30 days, ideally 90+ days
5. **Market Conditions**: Include ranging, trending, volatile periods

### Simulation Logic

```python
for each timestamp in backtest_period:
    # Check grid levels
    if price crosses buy_level:
        execute_buy(quantity, price)
        update_position_avg()
        place_stop_loss(avg_entry × (1 - max_loss_pct))
    
    if price crosses sell_level:
        execute_sell(quantity, price)
        update_position_avg()
        place_stop_loss(avg_entry × (1 + max_loss_pct))
    
    # Check stop-loss
    if position and price hits stop_loss:
        close_position(price)
        record_loss()
    
    # Apply funding
    if funding_timestamp:
        apply_funding(position_size, funding_rate)
    
    # Record metrics
    record_equity(timestamp, pnl, position, drawdown)
```

### Parameter Grid Search

```python
param_grid = {
    'levels': [3, 5, 7, 10],
    'spacing_pct': [0.25, 0.5, 0.75, 1.0, 1.5, 2.0],
    'max_loss_pct': [2.0, 3.0, 4.0, 5.0],  # Must exceed grid_range
    'leverage': [3, 5, 10],
    'balance_usage_pct': [70, 90]
}

# Filter invalid combinations
valid_params = []
for p in param_grid:
    grid_range = p['levels'] × p['spacing_pct']
    if p['max_loss_pct'] > grid_range:
        valid_params.append(p)

# Run backtest for each valid combination
results = []
for params in valid_params:
    metrics = run_backtest(params, historical_data)
    results.append({
        'params': params,
        'sharpe': metrics.sharpe,
        'profit': metrics.total_pnl,
        'max_dd': metrics.max_drawdown
    })

# Rank by different objectives
ranked_by_sharpe = sorted(results, key=lambda x: x['sharpe'], reverse=True)
ranked_by_profit = sorted(results, key=lambda x: x['profit'], reverse=True)
ranked_by_dd = sorted(results, key=lambda x: x['max_dd'])  # ascending
```

## Output Format

### Results Table

| Rank | Levels | Spacing | SL% | Lev | Sharpe | Profit | Max DD | Score |
|---|---|---|---|---|---|---|---|---|
| 1 | 5 | 0.5% | 3.0% | 5x | 2.1 | +$1,234 | -18% | 8.5 |
| 2 | 3 | 0.75% | 3.0% | 5x | 1.9 | +$1,456 | -22% | 7.8 |
| 3 | 7 | 0.3% | 2.5% | 3x | 2.3 | +$892 | -12% | 7.5 |

**Score** = (Sharpe × 2) + (Profit_Norm) - (DD_Norm)

### Equity Curve Visualization

- Daily cumulative PnL
- Drawdown periods shaded
- Mark major trades (entries/exits)

### Risk Profile Recommendations

```
Based on your risk appetite:

CONSERVATIVE (low DD, steady returns):
  → Config #15: 10 levels, 0.25% spacing, 2.5% SL, 3x leverage
  → Expected: Sharpe 2.3, +$850/mo, -12% max DD

BALANCED (best risk-adjusted):
  → Config #3: 5 levels, 0.5% spacing, 3.0% SL, 5x leverage
  → Expected: Sharpe 2.1, +$1,234/mo, -18% max DD

AGGRESSIVE (max profit, higher DD):
  → Config #7: 3 levels, 1.0% spacing, 5.0% SL, 10x leverage
  → Expected: Sharpe 1.4, +$2,100/mo, -35% max DD
```

## Implementation Plan

### Phase 1: Data Pipeline
- [ ] Fetch historical OHLCV from VALR (or alternative source)
- [ ] Fetch historical funding rates
- [ ] Store in efficient format (Parquet/SQLite)

### Phase 2: Backtest Engine
- [ ] Implement grid logic simulation
- [ ] Implement position tracking (avg entry, realized PnL)
- [ ] Implement stop-loss logic
- [ ] Implement funding calculations
- [ ] Calculate metrics (Sharpe, DD, profit factor)

### Phase 3: Parameter Optimization
- [ ] Grid search over valid parameter space
- [ ] Filter invalid combinations (SL < grid range)
- [ ] Parallel execution for speed
- [ ] Store results in database

### Phase 4: User Interface
- [ ] CLI for running backtests
- [ ] Risk profile selector (conservative/balanced/aggressive)
- [ ] Results ranking and comparison
- [ ] Export configs to bot-ready JSON

### Phase 5: Validation
- [ ] Walk-forward analysis (train/test split)
- [ ] Sensitivity analysis (parameter stability)
- [ ] Monte Carlo simulation (robustness check)

## Next Steps

1. **Build backtest engine** (Python or Rust)
2. **Source historical data** (VALR API or alternative)
3. **Run initial parameter sweep** (SOLUSDTPERP, 90 days)
4. **Generate risk-profiled recommendations**
5. **Integrate with live bot** (config auto-selection)

---

**Status**: Ready for implementation  
**Priority**: High (enables data-driven config decisions)  
**Estimated Effort**: 2-3 days for MVP
