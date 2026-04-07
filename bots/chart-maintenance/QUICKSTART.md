# Quick Start Guide

## For New Sessions

### 1. Verify Prerequisites
```bash
# Check Rust binary exists
ls -la /home/admin/.openclaw/workspace/bots/chart-maintenance/cm_bot

# Check secrets exist
cat /home/admin/.openclaw/secrets/cm_secrets.env | head -4

# Check config
cat /home/admin/.openclaw/workspace/bots/chart-maintenance/config.json | jq '.phase2_pairs_enabled'
```

### 2. Start Phase 2 (Spot Pairs)
```bash
cd /home/admin/.openclaw/workspace/bots/chart-maintenance
./cm_bot --phase2
```

### 3. Monitor
```bash
# Watch logs
tail -f logs/cm_bot_rust.log | grep -E "COMPLETE|EXTERNAL|Balance|CM1|CM2"

# Check state
cat state.json | jq '.maker_account'

# Check if running
ps aux | grep cm_bot
```

### 4. Stop
```bash
pkill -f "cm_bot"
```

---

## Common Tasks

### Add More Spot Pairs
Edit `config.json`:
```json
"phase2_pairs_enabled": [
  "LINKZAR",
  "BTCZAR",
  "ETHZAR"
]
```
Then restart bot.

### Check Account Balances
```bash
# Via VALR UI: https://www.valr.com → Subaccounts → CM1/CM2

# Or via API (needs signature - use Python helper)
python3 /home/admin/.openclaw/secrets/secrets.py get cm1_api_key
```

### Reset State (if needed)
```bash
rm state.json
./cm_bot --phase2  # Will create fresh state
```

### Rebuild After Code Changes
```bash
export PATH="$HOME/.rustup/toolchains/stable-x86_64-unknown-linux-gnu/bin:$PATH"
cd /home/admin/.openclaw/workspace/bots/chart-maintenance-rust
cargo build --release
cp target/release/cm_bot ../chart-maintenance/
pkill -f cm_bot && ./cm_bot --phase2 &
```

---

## What to Expect

### Normal Logs
```
✅ Fetched LINKZAR from API: pp=2, qp=8, min_qty=0.04, min_value=10
✅ Cycle COMPLETE: Both orders fully filled
Maker: CM1 (BUY) | Taker: CM2 (SELL)
```

### Balance Warnings (Normal)
```
⚠️  Balance insufficient for requested 2.5, falling back to min 0.04
```

### Skip Cycles (Low Balance)
```
❌ INSUFFICIENT BALANCE: requested=2.5, safe=0.02, min_qty=0.04. Skipping cycle.
```

---

## Troubleshooting Quick Fixes

| Problem | Fix |
|---------|-----|
| Bot not starting | Check secrets file exists, check binary permissions |
| Insufficient balance errors | Transfer more ZAR/LINK to CM1/CM2 |
| High external fill rate | Normal up to 20%, check liquidity if >50% |
| State file corrupted | Delete `state.json`, restart bot |
| WebSocket disconnects | Auto-reconnects, no action needed |

---

## Key Commands

```bash
# Start
./cm_bot --phase2

# Stop
pkill -f cm_bot

# Status
ps aux | grep cm_bot

# Logs
tail -100 logs/cm_bot_rust.log

# Rebuild
cargo build --release && cp target/release/cm_bot ../chart-maintenance/
```
