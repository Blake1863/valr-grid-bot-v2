# Bybit Trading Strategy

**Last Updated:** 2026-03-26  
**Goal:** Generate enough profit to cover server + API costs + modest returns  
**Target:** $50-100/month (conservative), scale if profitable

---

## Current Status (2026-03-26 23:30 UTC)

| Metric | Value |
|--------|-------|
| Starting Capital | $122.48 USDT |
| Current Equity | $109.76 USDT |
| Total Loss | -$12.72 (-10.4%) |
| Open Position | 0.1 SOL (~$9 dust) |
| Available Balance | ~$25 USDT |

**Post-mortem:** Grid bot ran without backtesting, proper position sizing, or risk limits. Got stopped out hard. Lesson learned.

---

## Rules (NON-NEGOTIABLE)

### 1. Position Sizing
| Risk Level | Max Position | Max Loss |
|------------|--------------|----------|
| **Conservative** | 20% of equity | 1% of equity |
| **Moderate** | 30% of equity | 1.5% of equity |
| **Aggressive** | 50% of equity | 2% of equity |

**Default:** Moderate (30% position, 1.5% max loss)

**Example on $110 account:**
- Max position: $33
- Max loss per trade: $1.65
- Stop loss distance determines position size

### 2. Leverage
| Pair | Max Leverage |
|------|--------------|
| BTCUSDT | 5x |
| ETHUSDT | 5x |
| SOLUSDT | 5x |
| Altcoins | 3x |

**Never exceed 5x.** Ever.

### 3. Stop Loss & Take Profit
- **ALWAYS set both TP and SL** before or immediately after entry
- **Minimum R:R ratio:** 1:2 (risk $1 to make $2)
- **Stop loss:** Based on technical levels, not arbitrary %
- **Take profit:** At least 2x the stop distance

**Example:**
- Entry: $100
- Stop: $98 (-2%)
- Target: $104 (+4%) minimum

### 4. Trade Frequency
- **Max trades per day:** 3
- **Max open positions:** 1 (focus on one trade at a time)
- **No revenge trading** after a loss — wait 1 hour minimum

### 5. Pairs
**Approved:**
- BTCUSDT (highest liquidity, tightest spreads)
- ETHUSDT (high liquidity)
- SOLUSDT (moderate liquidity, higher volatility)

**Not approved** without explicit approval:
- Altcoins with low liquidity
- ZAR pairs (use VALR for those)
- Anything with spread >10bps

### 6. Grid Bots
**NEVER run a grid bot without:**
1. Backtest results (minimum 14 days historical)
2. Defined max drawdown tolerance
3. Position sizing that survives -50% move
4. Documentation in this folder

---

## Strategy Types

### A. Momentum Scalping (Primary)
**Timeframe:** 15m - 1h charts  
**Hold time:** 1-4 hours  
**Target:** 1-3% per trade  
**Win rate target:** 55%+

**Entry signals:**
- Break of structure (higher high / lower low)
- Volume confirmation
- RSI divergence (optional)

**Exit:**
- TP at 2-3R (2-3x risk distance)
- SL at invalidation level
- Trail stop after 1.5R profit

### B. Mean Reversion (Secondary)
**Timeframe:** 5m - 15m charts  
**Hold time:** 30 min - 2 hours  
**Target:** 0.5-1.5% per trade  
**Win rate target:** 60%+

**Entry signals:**
- RSI <30 (long) or >70 (short)
- Price at support/resistance
- Volume spike + reversal candle

**Exit:**
- TP at mean (middle of range)
- SL beyond the extreme
- Quick exit if momentum continues against you

### C. Grid Bots (Tertiary — Use Sparingly)
**Condition:** Only in confirmed range-bound markets  
**Requirement:** Backtest first (see `backtest.py` in valr-grid-bot)  
**Spacing:** 0.3-0.5% for BTC/ETH, 0.5-1.0% for SOL  
**Levels:** 3-5 levels max  
**Position size:** 20% of equity max

---

## Risk Management

### Daily Loss Limit
- **Hard stop:** -5% of equity in a day
- **Soft stop:** -3% — pause and review

If you hit -5% in a day, **stop trading for 24 hours**.

### Weekly Review
Every Sunday (or 7 days from start):
1. Calculate total PnL
2. Review all trades (winners and losers)
3. Adjust strategy if win rate <45% or avg loss > avg win
4. Withdraw profits if equity >150% of starting

### Drawdown Protocol
| Drawdown | Action |
|----------|--------|
| -5% | Reduce position size by 50% |
| -10% | Stop trading for 48 hours, review all trades |
| -15% | Stop trading for 1 week, backtest strategy again |
| -20% | **SHUTDOWN** — strategy is broken, need full rebuild |

---

## Trade Execution Checklist

Before entering ANY trade:

- [ ] Pair is approved (BTC/ETH/SOL)
- [ ] Position size ≤30% of equity
- [ ] Leverage ≤5x
- [ ] Stop loss level defined (technical level, not arbitrary %)
- [ ] Take profit level defined (minimum 2R)
- [ ] R:R ratio ≥1:2
- [ ] No other open positions
- [ ] Haven't hit daily loss limit
- [ ] Trade logged in `trade-log.md` BEFORE entry

After entry:
- [ ] TP/SL set server-side via `/v5/position/trading-stop`
- [ ] Screenshot or log entry reason
- [ ] Set alert for TP/SL levels (if possible)

After exit:
- [ ] Log exit price, PnL, reason (TP/SL/manual)
- [ ] Update performance summary
- [ ] Note lessons learned

---

## Cost Basis

### What This Bot Needs to Cover
| Cost | Estimate |
|------|----------|
| Server (VPS) | ~$10-20/month |
| OpenClaw API calls | ~$5-10/month |
| **Total monthly** | **~$15-30** |
| **Daily target** | **~$0.50-1.00** |

### Profit Targets
| Timeframe | Conservative | Moderate | Aggressive |
|-----------|--------------|----------|------------|
| Daily | $0.50 | $1.00 | $2.00 |
| Weekly | $3.50 | $7.00 | $14.00 |
| Monthly | $15 | $30 | $60 |
| **Return %** | **12%/mo** | **25%/mo** | **50%/mo** |

**Realistic target:** Moderate ($30/month = 25% monthly return)

At 25% monthly:
- Starting $110 → $140 after 1 month
- Sustainable if win rate >50% and avg win > avg loss

---

## Metrics to Track

| Metric | Target | Current |
|--------|--------|---------|
| Win Rate | >50% | TBD |
| Avg Win | >1.5x Avg Loss | TBD |
| Profit Factor | >1.5 | TBD |
| Max Drawdown | <15% | 10.4% ⚠️ |
| Sharpe Ratio | >1.0 | TBD |
| Trades/Week | 5-15 | TBD |

---

## Emergency Procedures

### If Bot Crashes
1. Check `systemctl --user status bybit-monitor.service`
2. Check logs: `tail -100 bots/bybit-trading/logs/monitor.log`
3. Restart: `systemctl --user restart bybit-monitor.service`
4. Verify position still has TP/SL (check Bybit app or API)

### If API Keys Compromised
1. Revoke API key immediately in Bybit dashboard
2. Generate new key
3. Update secrets manager: `python3 secrets.py set bybit_api_key <new_key>`
4. Update cron/monitor if needed

### If Position Goes Against You
1. **DO NOT** move stop loss further away
2. **DO NOT** add to losing position (no martingale)
3. Let the stop loss hit if price reaches it
4. Review: was the entry valid? Was the SL at the right level?

### If You Hit -10% Drawdown
1. Stop trading for 48 hours
2. Review every trade in the log
3. Identify patterns: overtrading? ignoring SL? revenge trading?
4. Adjust rules if needed
5. Resume at 50% position size for first week back

---

## Notes

- **Document everything.** If it's not written down, it didn't happen.
- **No secrets.** All trades, wins and losses, go in the log.
- **Be honest.** Don't fudge numbers to make it look better.
- **This is a marathon, not a sprint.** Consistency > home runs.

---

## Revision History

| Date | Change |
|------|--------|
| 2026-03-26 | Initial strategy document created after grid bot blowup (-10.4%) |
