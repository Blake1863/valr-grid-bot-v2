#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(dirname "$SCRIPT_DIR")"

echo "🔨 Building Chart Maintenance Bot v2..."
cd "$BOT_DIR"

cargo build --release

echo "✅ Build complete!"
echo "Binary: $BOT_DIR/target/release/cm-bot-v2"

# Show binary info
ls -lh "$BOT_DIR/target/release/cm-bot-v2"
stat "$BOT_DIR/target/release/cm-bot-v2" | grep Modify
