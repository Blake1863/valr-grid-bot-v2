# VALR Auto Buy & Sweep Bot

Detects ZAR deposits → Buys SOL → VALR Pay → Stake

## Setup

### 1. Create Subaccount & API Keys

1. Create subaccount "AutobuySol" in VALR dashboard
2. Generate API keys with permissions:
   - **Read** (balances, orderbook)
   - **Trade** (market orders)
   - **Withdraw** (VALR Pay transfers)
   - **Earn** (staking)
3. Note the subaccount ID (e.g., `1234567890123456789`)

### 2. Add Secrets

```bash
# Add to Alibaba Secrets Manager
python3 /home/admin/.openclaw/secrets/secrets.py set valr_autobuy_api_key "<64-char-key>"
python3 /home/admin/.openclaw/secrets/secrets.py set valr_autobuy_api_secret "<secret>"
python3 /home/admin/.openclaw/secrets/secrets.py set valr_autobuy_subaccount_id "<subaccount-id>"
```

### 3. Install Dependencies

```bash
pip3 install requests
```

### 4. Configure

Edit `config.json` if needed (defaults are set per Blake's specs).

### 5. Deploy Systemd Service

```bash
# Copy service file
cp valr-auto-sweep.service ~/.config/systemd/user/

# Reload and enable
systemctl --user daemon-reload
systemctl --user enable valr-auto-sweep.service
systemctl --user start valr-auto-sweep.service

# Check status
systemctl --user status valr-auto-sweep.service
tail -f logs/auto-sweep.log
```

## API Endpoints (VALR)

### Confirmed Endpoints
- `GET /v1/account/balances` - Get balances
- `GET /v1/public/{pair}/orderbook` - Public orderbook
- `POST /v1/orders/{pair}/market` - Market order

### ✅ All Endpoints Confirmed

1. **VALR Pay Transfer** ✅
   - Endpoint: `POST /v1/pay`
   - Body: `{"currency": "SOL", "amount": 1.0, "recipientPayId": "M3Q5AEUP8W99QZR6T3ZS", "anonymous": "false"}`
   - Note: Amount is float

2. **SOL Staking** ✅
   - Endpoint: `POST /v1/staking/stake`
   - Body: `{"currencySymbol": "SOL", "amount": "1.0", "earnType": "STAKE"}`
   - Note: Amount is string, earnType="STAKE"

## Flow

```
1. Poll ZAR balance every 5 min
2. Detect delta > R1,000 (min trigger)
3. Calculate SOL buy amount (50bps max slippage)
4. If ZAR > R25,000: split into chunks, 10s apart
5. Execute market buy(s) with retry logic
6. Transfer 100% SOL via VALR Pay to M3Q5AEUP8W99QZR6T3ZS
7. Stake any remaining SOL
8. Log everything, alert on failures (3 retries max)
```

## Logs

- Activity: `logs/auto-sweep.log`
- Stdout: `logs/auto-sweep.stdout`
- Stderr: `logs/auto-sweep.stderr`

## State

- `state.json` - Tracks last known ZAR balance (prevents double-processing)

## Telegram Alerts

Currently logs alerts with `TELEGRAM ALERT:` prefix. To be integrated with OpenClaw message tool for actual Telegram delivery.

---

**Built for Blake @ VALR** 🤖
