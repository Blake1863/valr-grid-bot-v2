#!/bin/bash
# VALR Grid Bot v3 - ETH Startup Script
# Loads secrets from secrets.py and starts the bot with ETH config

set -e

cd /home/admin/.openclaw/workspace/bots/valr-grid-bot-v3

# Load secrets from secrets.py
export VALR_API_KEY=$(python3 /home/admin/.openclaw/secrets/secrets.py get valr_main_api_key 2>/dev/null)
export VALR_API_SECRET=$(python3 /home/admin/.openclaw/secrets/secrets.py get valr_main_api_secret 2>/dev/null)

# Verify secrets are loaded
if [ -z "$VALR_API_KEY" ]; then
    echo "ERROR: VALR_API_KEY not loaded"
    exit 1
fi
if [ -z "$VALR_API_SECRET" ]; then
    echo "ERROR: VALR_API_SECRET not loaded"
    exit 1
fi

echo "Starting VALR Grid Bot v3 (ETHUSDTPERP)..."
echo "API Key: ${VALR_API_KEY:0:8}..."
echo "Config: configs/eth-config.json"

# Start the bot with ETH config
export VALR_GRID_BOT_CONFIG="configs/eth-config.json"
exec /usr/bin/node dist/app/main.js
