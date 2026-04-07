# cm-bot-v2 — VALR Perp Futures Chart Maintenance Bot

Wash trades 6 perpetual futures pairs between CM1 and CM2 subaccounts.

## Architecture

- **Language**: Rust
- **Order placement**: Account WebSocket (`PLACE_LIMIT_ORDER` → `PLACE_LIMIT_WS_RESPONSE`)
- **Pricing**: Trade WebSocket (`AGGREGATED_ORDERBOOK_UPDATE`), mark price fallback if spread >50bps
- **Balance**: `totalInReference` from `BALANCE_UPDATE` WS event
- **Rotation**: 6-cycle window — 3 cycles CM1 sells/CM2 buys, 3 cycles CM2 sells/CM1 buys (net=0 in 90s)

## Active Pairs

| Pair          | Min Qty  |
|---------------|----------|
| BTCUSDTPERP   | 0.0001   |
| ETHUSDTPERP   | 0.001    |
| XRPUSDTPERP   | 2.0      |
| DOGEUSDTPERP  | 6.0      |
| SOLUSDTPERP   | 0.01     |
| AVAXUSDTPERP  | 0.03     |

## Subaccounts

| Account | Subaccount ID       |
|---------|---------------------|
| CM1     | 1483472097578319872 |
| CM2     | 1483472079069155328 |

## Files

```
src/
  main.rs       — cycle loop, balance checks, startup
  ws_client.rs  — account WS (order placement + balance updates)
  price_feed.rs — trade WS (orderbook prices)
  cycle.rs      — order execution logic
  cleanup.rs    — periodic stale order cancellation
  state.rs      — cycle count persistence
  config.rs     — config loading
  pair_info.rs  — VALR pair info fetch
  types.rs      — shared types
config.json     — pairs, interval, multiplier settings
state.json      — cycle counts (auto-generated)
logs/           — stdout/stderr
```

## Service

```bash
systemctl --user status cm-bot-v2.service
systemctl --user restart cm-bot-v2.service
tail -f logs/cm-bot-v2.log
```

## Build & Deploy

```bash
~/.cargo/bin/cargo build --release
stat target/release/cm-bot-v2 | grep Modify   # verify timestamp changed
systemctl --user restart cm-bot-v2.service
```

## Config

```json
{
  "pairs": { "SOLUSDTPERP": { "enabled": true, "type": "perp" } },
  "cycle_interval_ms": 15000,
  "qty_range_min_multiplier": 1.0,
  "qty_range_max_multiplier": 1.5
}
```

## Monitoring

Key log patterns:
- `[INFO] Phase 0: CM1 Sell (maker) vs CM2 Buy (taker)` — rotation working
- `[INFO] WS order confirmed` — maker placed on book
- `[INFO] Cycle recorded` — both sides filled
- `[ERROR] Taker failed` — taker missed, maker cancelled automatically
- `[WARN] WS disconnected` — reconnecting (ping/pong keeps connection alive, reconnect on close)
