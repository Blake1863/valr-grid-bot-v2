#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_NAME="cm-bot-v2"

echo "🚀 Deploying Chart Maintenance Bot v2..."

# Build first
"$SCRIPT_DIR/build.sh"

# Create systemd service file
SERVICE_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
mkdir -p "$(dirname "$SERVICE_FILE")"

cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Chart Maintenance Bot v2 (Rust)
After=network.target

[Service]
Type=simple
User=admin
WorkingDirectory=$BOT_DIR
Environment="CM1_API_KEY=\$(cat /home/admin/.openclaw/secrets/cm_secrets.env | grep CM1_API_KEY | cut -d'=' -f2)"
Environment="CM1_API_SECRET=\$(cat /home/admin/.openclaw/secrets/cm_secrets.env | grep CM1_API_SECRET | cut -d'=' -f2)"
Environment="CM2_API_KEY=\$(cat /home/admin/.openclaw/secrets/cm_secrets.env | grep CM2_API_KEY | cut -d'=' -f2)"
Environment="CM2_API_SECRET=\$(cat /home/admin/.openclaw/secrets/cm_secrets.env | grep CM2_API_SECRET | cut -d'=' -f2)"
ExecStart=$BOT_DIR/target/release/cm-bot-v2
Restart=always
RestartSec=5
StandardOutput=append:$BOT_DIR/logs/cm-bot-v2.stdout
StandardError=append:$BOT_DIR/logs/cm-bot-v2.stderr

[Install]
WantedBy=default.target
EOF

echo "✅ Service file created: $SERVICE_FILE"

# Reload systemd and enable service
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"

echo "✅ Deployed! Start with: systemctl --user start $SERVICE_NAME"
echo "   Status: systemctl --user status $SERVICE_NAME"
echo "   Logs: journalctl --user -u $SERVICE_NAME -f"
