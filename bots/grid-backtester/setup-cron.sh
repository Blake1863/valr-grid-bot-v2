#!/bin/bash
# Setup weekly cron job for grid bot backtester
# Runs every Sunday at 20:00 SAST (18:00 UTC)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="/home/admin/.openclaw/workspace"
VENV="$SCRIPT_DIR/venv"

echo "Setting up grid backtester cron job..."

# Create virtual environment if it doesn't exist
if [ ! -d "$VENV" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv "$VENV"
fi

# Install dependencies
echo "Installing dependencies..."
"$VENV/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"

# Create cron job (14 days, 1-hour candles)
CRON_CMD="cd $SCRIPT_DIR && $VENV/bin/python backtest.py --pair SOLUSDTPERP --days 14 --output $WORKSPACE/bots/grid-backtester/results >> $WORKSPACE/bots/grid-backtester/logs/cron.log 2>&1"

# Check if cron already exists
if crontab -l 2>/dev/null | grep -q "grid-backtester/backtest.py"; then
    echo "⚠️  Cron job already exists. Remove with: crontab -e"
    crontab -l | grep "grid-backtester"
else
    # Add to crontab (Sunday 20:00 SAST = 18:00 UTC)
    (crontab -l 2>/dev/null | grep -v "grid-backtester"; echo "0 18 * * 0 $CRON_CMD") | crontab -
    echo "✅ Cron job added:"
    echo "   Schedule: Every Sunday at 20:00 SAST (18:00 UTC)"
    echo "   Command: $CRON_CMD"
fi

# Create logs directory
mkdir -p "$SCRIPT_DIR/logs"

echo ""
echo "To run manually: cd $SCRIPT_DIR && $VENV/bin/python backtest.py"
echo "To view cron: crontab -l"
echo "To remove cron: crontab -e (delete the grid-backtester line)"
