# Setup Checklist for Blake

## ✅ What's Built

- **Bot code:** `main.py` - Full orchestration (detect → buy → pay → stake)
- **Config:** `config.json` - All your specs loaded (5min poll, R25k splits, 50bps slippage, etc.)
- **Systemd service:** `valr-auto-sweep.service` - Auto-start on boot
- **Test script:** `test_endpoints.py` - Verify API access before deploying
- **Logging:** `logs/` directory ready

## 🔑 What You Need To Do

### 1. Create Subaccount & API Keys
- [ ] Create subaccount "AutobuySol" in VALR dashboard
- [ ] Generate API keys with permissions:
  - [ ] Read (balances)
  - [ ] Trade (market orders)
  - [ ] Withdraw (VALR Pay)
  - [ ] Earn (staking)
- [ ] Note the subaccount ID (e.g., `1483815480334401536`)

### 2. Add Secrets
```bash
python3 /home/admin/.openclaw/secrets/secrets.py set valr_autobuy_api_key "<64-char-key>"
python3 /home/admin/.openclaw/secrets/secrets.py set valr_autobuy_api_secret "<secret>"
python3 /home/admin/.openclaw/secrets/secrets.py set valr_autobuy_subaccount_id "<subaccount-id>"
```

### 3. Confirm API Endpoints (Internal Docs)
- [ ] **VALR Pay:** Confirm `POST /v1/pay` payload structure
  - Expected: `{"currency": "SOL", "amount": "...", "identifier": "M3Q5AEUP8W99QZR6T3ZS", "type": "PAY"}`
- [ ] **Staking:** Confirm exact endpoint and payload
  - Assumed: `POST /v1/earn/stake` but need internal docs

### 4. Test Endpoints
```bash
cd /home/admin/.openclaw/workspace/bots/valr-auto-sweep
python3 test_endpoints.py
```
- [ ] All endpoints return 200/400 (not 401/403)

### 5. Deploy
```bash
# Copy service file
cp valr-auto-sweep.service ~/.config/systemd/user/

# Reload and start
systemctl --user daemon-reload
systemctl --user enable valr-auto-sweep.service
systemctl --user start valr-auto-sweep.service

# Verify
systemctl --user status valr-auto-sweep.service
tail -f logs/auto-sweep.log
```

### 6. Fund & Test
- [ ] Send small ZAR amount (e.g., R100) to AutobuySol subaccount
- [ ] Wait for next poll (5 min)
- [ ] Check logs for:
  - Deposit detected
  - SOL purchased
  - VALR Pay transfer complete
  - Staking complete
- [ ] Verify wife received SOL via VALR Pay
- [ ] Verify staking shows in VALR Earn

## 📋 Config Summary

| Setting | Value |
|---------|-------|
| Poll interval | 300s (5 min) |
| Split threshold | R25,000 |
| Split delay | 10s |
| Max slippage | 50bps (0.5%) |
| Pair | SOLZAR |
| VALR Pay ID | M3Q5AEUP8W99QZR6T3ZS |
| Min deposit trigger | R1,000 |
| Max retries | 3 |
| Telegram topic | 21 |

## 🚨 Alerts

Bot will alert Telegram topic #21 if:
- Buy order fails after 3 retries
- VALR Pay transfer fails after 3 retries
- Staking fails after 3 retries
- Any unexpected error in main loop

---

**Ready when you are!** Just add the secrets and confirm the API endpoints, then we deploy. 🤖
