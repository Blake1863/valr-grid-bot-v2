# cm-bot-spot — VALR Spot Chart Maintenance Bot

Wash trades 19 spot pairs between CMS1 and CMS2 subaccounts.

## Architecture

- **Language**: Rust
- **Order placement**: Account WebSocket (`PLACE_LIMIT_ORDER` → `PLACE_LIMIT_WS_RESPONSE`)
- **Pricing**: Trade WebSocket (`AGGREGATED_ORDERBOOK_UPDATE`), mark price fallback if spread >50bps
- **Balance**: No pre-flight check — multi-asset bot can't track all base assets in WS state. VALR rejects via `ORDER_FAILED` if insufficient.
- **Rotation**: 6-cycle window — 3 cycles CMS1 sells/CMS2 buys, 3 cycles CMS2 sells/CMS1 buys (net=0 in 90s)
- **Rebalancer**: Every 6 cycles per pair — transfers if either account has >60% of combined holdings

## Active Pairs (19)

**ZAR pairs**: LINKZAR, BTCZAR, ETHZAR, XRPZAR, SOLZAR, AVAXZAR, BNBZAR  
**USDT pairs**: BTCUSDT, ETHUSDT, XRPUSDT, SOLUSDT, AVAXUSDT, BNBUSDT, LINKUSDT  
**USDC pairs**: BTCUSDC, ETHUSDC, SOLUSDC, AVAXUSDC, EURCUSDC

## Subaccounts

| Account | Subaccount ID       |
|---------|---------------------|
| CMS1    | 1483815480334401536 |
| CMS2    | 1483815498551132160 |

## Files

```
src/
  main.rs        — cycle loop, startup, rebalancer trigger
  ws_client.rs   — account WS (order placement + ZAR/base balance updates)
  price_feed.rs  — trade WS (orderbook prices)
  rebalance.rs   — inventory rebalancer (REST-based, fires every 6 cycles)
Cargo.toml
.env            — MAIN_API_KEY, MAIN_API_SECRET, CM1/CM2_SUBACCOUNT_ID
```

Deploy binary to:
```
../cm-bot-spot/cm-bot-spot
../cm-bot-spot/config.json
../cm-bot-spot/logs/
```

## Service

```bash
systemctl --user status cm-bot-spot.service
systemctl --user restart cm-bot-spot.service
tail -f ../cm-bot-spot/logs/cm-bot-spot.log
```

## Build & Deploy

```bash
~/.cargo/bin/cargo build --release
systemctl --user stop cm-bot-spot.service
cp target/release/cm-bot-spot ../cm-bot-spot/cm-bot-spot
systemctl --user start cm-bot-spot.service
```

## Config (../cm-bot-spot/config.json)

```json
{
  "pairs": {
    "LINKZAR": { "enabled": true, "type": "spot" },
    "EURCUSDC": { "enabled": true, "type": "spot" }
  },
  "cycle_interval_ms": 15000,
  "cleanup_interval_ms": 60000,
  "cleanup_age_threshold_ms": 45000,
  "qty_range_min_multiplier": 1.0,
  "qty_range_max_multiplier": 4.0
}
```

To add a new pair: add entry to config, fund base+quote on both CMS1/CMS2, restart.

## ZAR Funding

ZAR pairs require ZAR in both subaccounts. Zero ZAR = all ZAR pairs skip.

**Top up process:**
1. Add ZAR to VALR via bank transfer (VALR app)
2. Transfer from Primary to CMS1: `POST /v1/account/subaccounts/transfer` with `currencyCode: "ZAR"`
3. Transfer half to CMS2
4. Note: USDTZAR has low liquidity — IOC orders won't fill reliably

## Monitoring

Key log patterns:
- `[INFO] Phase 0: CMS1 SELL (maker)` — rotation working
- `[INFO] CMS1 Maker placed / CMS2 Taker placed` — self-match confirmed
- `[INFO] Cycle recorded for LINKZAR: cycle #N` — successful cycle
- `[REBALANCE] XRP imbalanced: CMS1=49 CMS2=17` — rebalancer triggered
- `[REBALANCE] ✅ XRP transferred` — rebalance complete
- `[ERROR] ORDER_FAILED` — VALR rejected order (usually insufficient balance, rebalancer will fix)

## Known Limitations

- WS `SharedBalance` only tracks the first pair's base currency — not used for balance checks
- ZAR pairs are vulnerable to depletion if not topped up regularly
- USDTZAR is illiquid — can't buy ZAR programmatically; must use VALR app
