# VALR Bot Deployment - Current State

**Last Updated:** 2026-03-24 21:23 SAST

---

## Credential Architecture

All credentials stored in **local encrypted vault** (`~/.openclaw/secrets/secrets.py`), NOT Alibaba Secrets Manager.

| Secret Name | Purpose | Account | Balance |
|-------------|---------|---------|---------|
| `valr_grid_bot_1_api_key` / `valr_grid_bot_1_api_secret` | Grid Bot | Dedicated grid bot account | ~USDT 16 |
| `valr_main_api_key` / `valr_main_api_secret` | CM Bots | CMSpot-eead (main account) | ~USDT 277 |
| `bybit_api_key` / `bybit_api_secret` | Bybit | Bybit account | - |

### Subaccount Mappings

| Subaccount | ID | Purpose | Bot |
|------------|----|---------|-----|
| **CM1** | `1483472097578319872` | Futures trading | cm-bot-v2 |
| **CM2** | `1483472079069155328` | Futures trading | cm-bot-v2 |
| **CMS1** | `1483815480334401536` | Spot trading | cm-bot-spot |
| **CMS2** | `1483815498551132160` | Spot trading | cm-bot-spot |

---

## Bot Status

| Bot | Service | Status | Credentials | Subaccounts |
|-----|---------|--------|-------------|-------------|
| **valr-grid-bot** | `valr-grid-bot.service` | ✅ Running | `valr_grid_bot_1_*` | Direct account |
| **cm-bot-v2** | `cm-bot-v2.service` | ✅ Running | `valr_main_*` | CM1, CM2 |
| **cm-bot-spot** | `cm-bot-spot.service` | ✅ Running | `valr_main_*` | CMS1, CMS2 |

### Current Issues

**⚠️ WebSocket Rate Limiting (HTTP 429)**
- CM bots (v2 & spot) experiencing WS connection instability
- Cause: Extensive testing triggered VALR rate limits
- Impact: Order placement timing out, WS connections dropping
- Resolution: Automatic recovery once rate limit window expires (1-5 min)
- REST API working fine (balance checks, etc.)

**Grid Bot:** Fully operational, managing SOLUSDTPERP position with stop-loss.

---

## Configuration Files

### cm-bot-v2 (Futures)
- **Binary:** `bots/cm-bot-v2/cm-bot-v2`
- **Config:** `bots/cm-bot-v2/config.json`
- **Env:** `bots/cm-bot-v2/.env` (subaccount IDs only)
- **Logs:** `bots/cm-bot-v2/logs/cm-bot-v2.log`

### cm-bot-spot (Spot)
- **Binary:** `bots/cm-bot-spot/cm-bot-spot`
- **Config:** `bots/cm-bot-spot/config.json`
- **Env:** `bots/cm-bot-spot/.env` (subaccount IDs only)
- **Logs:** `bots/cm-bot-spot/logs/cm-bot-spot.log`

### valr-grid-bot
- **Binary:** `bots/valr-grid-bot/valr-grid-bot`
- **Config:** `bots/valr-grid-bot/config.json`
- **Logs:** `bots/valr-grid-bot/logs/grid-bot.stdout`

---

## Service Management

```bash
# Status
systemctl --user status valr-grid-bot.service cm-bot-v2.service cm-bot-spot.service

# Restart individual
systemctl --user restart valr-grid-bot.service
systemctl --user restart cm-bot-v2.service
systemctl --user restart cm-bot-spot.service

# Restart all
systemctl --user restart valr-grid-bot.service cm-bot-v2.service cm-bot-spot.service

# Logs
journalctl --user -u valr-grid-bot.service -f
tail -f bots/cm-bot-v2/logs/cm-bot-v2.log
tail -f bots/cm-bot-spot/logs/cm-bot-spot.log
```

---

## Credential Management

```bash
# List secrets
python3 ~/.openclaw/secrets/secrets.py list

# Get secret
python3 ~/.openclaw/secrets/secrets.py get valr_main_api_key

# Set secret
python3 ~/.openclaw/secrets/secrets.py set <name> <value>

# Delete secret
python3 ~/.openclaw/secrets/secrets.py delete <name>
```

---

## Security Notes

✅ No plaintext API keys in workspace files
✅ All credentials encrypted with Fernet (local vault)
✅ Subaccount IDs only in .env files (non-sensitive)
✅ Main account used for subaccount impersonation (CM bots)
✅ Dedicated credentials for grid bot (isolation)

---

## Known Issues / TODO

1. **WS Rate Limiting** - CM bots experiencing 429 errors (auto-recovering)
2. **cm-bot-v2 WS Auth** - Signature format adjusted (removed subaccount from WS signature, using main account directly like valr-common)
3. **Build Issues** - ws_client.rs had syntax errors from editing (resolved)

---

## Next Steps

- Monitor CM bots for WS recovery
- Verify order placement working after rate limit clears
- Consider adding rate limiting to bot WS reconnection logic
