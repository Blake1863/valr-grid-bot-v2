# Grid Bot Backtester

Weekly backtester for VALR grid bot optimization.

## What it does

- Fetches **14 days of 1-hour OHLCV candles** from VALR (336 data points)
- Tests **stop-loss % optimization** (2%, 3%, 5%, 7.5%, 10%, 15%, 20%, 25%)
- Simulates grid trading with different spacing/levels combinations
- Outputs optimal parameters based on:
  - **Best by Sharpe** — Risk-adjusted returns (recommended)
  - **Best by PnL** — Raw profit (may have higher drawdown)
  - **Best Efficiency** — Highest PnL per unit of drawdown

## Usage

### Manual run

```bash
cd /home/admin/.openclaw/workspace/bots/grid-backtester

# Create venv (first time only)
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# Run backtest
./venv/bin/python backtest.py --pair SOLUSDTPERP --days 14
```

### Automated (weekly)

```bash
# Setup cron job (runs Sundays at 20:00 SAST)
./setup-cron.sh

# View scheduled jobs
crontab -l

# Remove cron
crontab -e  # delete the grid-backtester line
```

## Output

- `results/{pair}_{timestamp}.json` — Full report with all metrics
- `results/{pair}_{timestamp}.csv` — All tested combinations for analysis

## Parameters tested

- **Levels**: 3, 4, 5, 6, 8, 10, 12
- **Spacing**: 0.1%, 0.15%, 0.2%, 0.25%, 0.3%, 0.4%, 0.5%, 0.75%, 1.0%
- **Leverage**: 5x (fixed)
- **Balance usage**: 90% (fixed)

## Metrics

| Metric | Description |
|--------|-------------|
| `total_pnl` | Total profit/loss over the period |
| `max_drawdown_pct` | Maximum peak-to-trough decline |
| `total_fills` | Number of grid fills executed |
| `win_rate_pct` | Percentage of profitable fills |
| `sharpe_ratio` | Risk-adjusted return (mean/std of fill PnLs) |

## Updating config

After running the backtest, review the results and update `../valr-grid-bot/config.json`:

```json
{
  "levels": 6,        // From backtest recommendation
  "spacing_pct": 0.25 // From backtest recommendation
}
```

The grid bot will pick up changes on the next re-centre (~30 min) — no restart needed.

## Notes

- Backtest uses a simplified fill model (assumes fills at grid price when high/low crosses level)
- Real performance may differ due to:
  - Order book depth
  - Slippage on fills
  - Fee differences (backtest assumes 0% maker fee)
  - Market impact
- Use as a guide, not gospel — always start with conservative params
