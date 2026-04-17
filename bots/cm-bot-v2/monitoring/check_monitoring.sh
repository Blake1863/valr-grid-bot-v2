#!/bin/bash
# CM Bot v2 Monitoring Check Script
# Run this 1 hour after baseline to detect anomalies

BASELINE="/home/admin/.openclaw/workspace/bots/cm-bot-v2/monitoring/baseline_2026-04-17_18-40.json"
LOG="/home/admin/.openclaw/workspace/bots/cm-bot-v2/logs/cm-bot-v2.log"
STATE="/home/admin/.openclaw/workspace/bots/cm-bot-v2/state.json"

echo "=== CM Bot v2 Monitoring Check ==="
echo "Time: $(date -Iseconds)"
echo ""

# Get current fill count
CURRENT_FILLS=$(grep -c "\[FILL\]" "$LOG" 2>/dev/null || echo 0)
echo "Current total fills: $CURRENT_FILLS"

# Check for external fills
EXTERNAL=$(grep -c "external_fills.*[1-9]" "$STATE" 2>/dev/null || echo 0)
echo "External fills detected: $EXTERNAL"

# Get current balances
echo ""
echo "Current Balances:"
export VALR_API_KEY=eead9a0d3c756af711a0474d2f594f6e36251aa603c4ca65d21d7265894d8362
export VALR_API_SECRET=9770a04c64215cb4ccaf7f698903a0dc64fea6f21d519985515ae05abb2d66db

CM1=$(python3 /home/admin/.openclaw/workspace/skills/valr-exchange/scripts/valr_request.py GET /v1/account/balances --subaccount-id 1483472097578319872 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); usdt=[x for x in d if x['currency']=='USDT'][0]; print(f\"CM1: {usdt['total']} USDT (avail: {usdt['available']})\")" 2>/dev/null)
CM2=$(python3 /home/admin/.openclaw/workspace/skills/valr-exchange/scripts/valr_request.py GET /v1/account/balances --subaccount-id 1483472079069155328 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); usdt=[x for x in d if x['currency']=='USDT'][0]; print(f\"CM2: {usdt['total']} USDT (avail: {usdt['available']})\")" 2>/dev/null)

echo "$CM1"
echo "$CM2"
echo ""

# Check for anomalies in recent logs
echo "Recent log anomalies:"
RECENT_ERRORS=$(tail -1000 "$LOG" 2>/dev/null | grep -ci "error\|failed\|rejected" || echo 0)
echo "  Error/failed messages: $RECENT_ERRORS"

EXTERNAL_LOG=$(tail -1000 "$LOG" 2>/dev/null | grep -ci "external" || echo 0)
echo "  External mentions: $EXTERNAL_LOG"

echo ""
echo "=== End Check ==="
