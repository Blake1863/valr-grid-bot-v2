# VALR Bot Deployment - Current State

**Last Updated:** 2026-04-21 04:36 Asia/Shanghai

---

## Credential Architecture

All credentials stored in **local encrypted vault** (`~/.openclaw/secrets/secrets.py`), NOT Alibaba Secrets Manager.

| Secret Name | Purpose | Account | Balance |
|-------------|---------|---------|---------|
| `valr_main_api_key` / `valr_main_api_secret` | All Bots | primary account (main account) | ~USDT 277 |
| `bybit_api_key` / `bybit_api_secret` | Bybit | Bybit account | - |

**Note:** `valr_grid_bot_1_*` credentials deprecated — all bots now use `primary account` key with subaccount impersonation.

### Subaccount Mappings

| Subaccount | ID | Purpose | Bot |
|------------|----|---------|-----|
| **CM1** | `1483472097578319872` | Futures trading | cm-bot-v2 |
| **CM2** | `1483472079069155328` | Futures trading | cm-bot-v2 |
| **CMS1** | `1483815480334401536` | Spot trading | cm-bot-spot |
| **CMS2** | `1483815498551132160` | Spot trading | cm-bot-spot |
| **Grid Bot 1** | `1432690254033137664` | Futures grid | valr-grid-bot-v3 (SOL) |
| **Grid Bot 2** | `1491067064373735424` | Futures grid | valr-grid-bot-v3-eth (ETH) |

---

## Bot Status

| Bot | Service | Status | Credentials | Subaccounts |
|-----|---------|--------|-------------|-------------|
| **valr-grid-bot-v3** | `valr-grid-bot-v3.service` | ✅ ACTIVE | `valr_main_*` | Grid Bot 1 (SOL) |
| **valr-grid-bot-v3-eth** | `valr-grid-bot-v3-eth.service` | ✅ ACTIVE | `valr_main_*` | Grid Bot 2 (ETH) |
| **valr-grid-bot-v2** | `valr-grid-bot-v2.service` | ⛔ DEPRECATED | — | — |
| **valr-grid-bot** | `valr-grid-bot.service` | ⛔ DEPRECATED | — | — |
| **cm-bot-v2** | `cm-bot-v2.service` | ✅ Running | `valr_main_*` | CM1, CM2 |
| **cm-bot-spot** | `cm-bot-spot.service` | ✅ Running | `valr_main_*` | CMS1, CMS2 |

---

## Configuration Files

### valr-grid-bot-v3 (SOL)
- **Config:** `bots/valr-grid-bot-v3/configs/bot-config.json`
- **Logs:** `bots/valr-grid-bot-v3/logs/bot.log`
- **State:** `bots/valr-grid-bot-v3/logs/solusdtperp-state.db`

### valr-grid-bot-v3-eth (ETH)
- **Config:** `bots/valr-grid-bot-v3/configs/eth-config.json`
- **Logs:** `bots/valr-grid-bot-v3/logs/bot.log`
- **State:** `bots/valr-grid-bot-v3/logs/ethusdtperp-state.db`

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

---

## Service Management

```bash
# Status - Grid Bots
systemctl --user status valr-grid-bot-v3.service valr-grid-bot-v3-eth.service

# Status - CM Bots
systemctl --user status cm-bot-v2.service cm-bot-spot.service

# Restart individual
systemctl --user restart valr-grid-bot-v3.service
systemctl --user restart valr-grid-bot-v3-eth.service
systemctl --user restart cm-bot-v2.service
systemctl --user restart cm-bot-spot.service

# Logs
journalctl --user -u valr-grid-bot-v3.service -f
journalctl --user -u valr-grid-bot-v3-eth.service -f
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
✅ Main account (`primary account`) used for all subaccount impersonation
✅ Deprecated credentials can be removed from vault

---

## Deprecated Services

The following services have been stopped and disabled:

| Service | Reason | Action |
|---------|--------|--------|
| `valr-grid-bot.service` | v1 Rust bot — obsolete | Stopped, disabled |
| `valr-grid-bot-v2.service` | v2 SOL bot — replaced by v3 | Stopped, disabled |
| `valr-grid-bot-v2-eth.service` | v2 ETH bot — replaced by v3 | Stopped, disabled |

---

## Known Issues / TODO

1. **WS Rate Limiting** - CM bots may experience 429 errors (auto-recovering)
2. **Cleanup** - Remove deprecated `valr_grid_bot_1_*` credentials from vault

---

## Next Steps

- Monitor v3 grid bots for stability
- Consider removing deprecated credentials from vault
- Verify CM bot WS stability after rate limit recovery
