#!/bin/bash
# VALR Futures Grid Bot - Setup Script
# 
# This script sets up the bot environment:
# 1. Creates vault directory and encryption key
# 2. Installs Python dependencies
# 3. Builds the Rust binary
# 4. Validates configuration

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(dirname "$SCRIPT_DIR")"
VAULT_DIR="$HOME/.valr-bot"

echo "🤖 VALR Futures Grid Bot Setup"
echo "=============================="
echo

# Step 1: Create vault directory and key
echo "📁 Setting up secrets vault..."
mkdir -p "$VAULT_DIR"
chmod 700 "$VAULT_DIR"

if [ ! -f "$VAULT_DIR/.key" ]; then
    echo "   Generating encryption key..."
    openssl rand -base64 32 > "$VAULT_DIR/.key"
    chmod 600 "$VAULT_DIR/.key"
    echo "   ✅ Key created: $VAULT_DIR/.key"
else
    echo "   ✅ Key exists: $VAULT_DIR/.key"
fi

# Step 2: Install Python dependencies
echo
echo "🐍 Installing Python dependencies..."
if ! python3 -c "import cryptography" 2>/dev/null; then
    echo "   Installing cryptography package..."
    pip3 install --user cryptography
    echo "   ✅ cryptography installed"
else
    echo "   ✅ cryptography already installed"
fi

# Step 3: Initialize secrets (if not already done)
echo
echo "🔐 Checking credentials..."
if [ ! -f "$VAULT_DIR/vault.json" ]; then
    echo "   ⚠️  Vault not initialized"
    echo
    echo "   Please run:"
    echo "   python3 $SCRIPT_DIR/secrets.py init <YOUR_API_KEY> <YOUR_API_SECRET>"
    echo
    echo "   Get API keys from: https://rooibos.dev"
    echo
    read -p "Initialize now? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        read -p "VALR API Key: " api_key
        read -s -p "VALR API Secret: " api_secret
        echo
        python3 "$SCRIPT_DIR/secrets.py" init "$api_key" "$api_secret"
    fi
else
    echo "   ✅ Vault initialized"
fi

# Step 4: Build Rust binary
echo
echo "🦀 Building Rust binary..."
if ! command -v cargo &> /dev/null; then
    echo "   ❌ Rust not found"
    echo "   Install from: https://rustup.rs"
    echo "   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
    exit 1
fi

cd "$BOT_DIR"
cargo build --release

echo "   ✅ Binary built: $BOT_DIR/target/release/valr-futures-grid-bot"

# Step 5: Validate config
echo
echo "📋 Validating configuration..."
if [ -f "$BOT_DIR/config/config.json" ]; then
    python3 "$BOT_DIR/scripts/validate_config.py" "$BOT_DIR/config/config.json"
    if [ $? -eq 0 ]; then
        echo
        echo "   ✅ Config validated successfully"
    else
        echo
        echo "   ⚠️  Config has issues. Review warnings above."
        read -p "   Continue anyway? [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "   Aborting. Fix config and re-run setup."
            exit 1
        fi
    fi
else
    echo "   ⚠️  Config not found - using defaults"
fi

# Done
echo
echo "=============================="
echo "✅ Setup complete!"
echo
echo "To run the bot:"
echo "  cd $BOT_DIR"
echo "  ./target/release/valr-futures-grid-bot"
echo
echo "Logs will appear in: $BOT_DIR/logs/"
echo
echo "To edit config:"
echo "  nano $BOT_DIR/config/config.json"
echo
