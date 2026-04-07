# VALR Futures Grid Bot - Package Info

## What's Included

```
valr-futures-grid-bot.tar.gz (12 KB)
├── Cargo.toml              # Rust project config
├── README.md               # Full documentation
├── .gitignore              # Git exclusions
├── config/
│   └── config.json         # Grid parameters (edit this!)
├── docs/
│   └── QUICKSTART.md       # 5-minute setup guide
├── scripts/
│   ├── secrets.py          # Encrypted credential storage
│   └── setup.sh            # One-command setup
└── src/
    └── main.rs             # Bot source code
```

## Distribution

Share the `valr-futures-grid-bot.tar.gz` file with friends. They need:

1. **Rust** (curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh)
2. **Python 3** with `cryptography` package
3. **VALR API keys** (futures trading enabled)

## Quick Deploy (for your friends)

```bash
# 1. Extract
tar -xzf valr-futures-grid-bot.tar.gz
cd valr-futures-grid-bot

# 2. Run setup (handles everything)
chmod +x scripts/setup.sh
./scripts/setup.sh

# 3. Run bot
./target/release/valr-futures-grid-bot
```

## Features

| Feature | Description |
|---|---|
| **Symmetric Grid** | Balanced buy/sell orders around mid-price |
| **Native Stop-Loss** | VALR conditional orders (exchange-monitored) |
| **Trailing Stop** | Auto-adjusts as position avg entry changes |
| **Post-Only** | Maker rebates (0% fees) |
| **Hot-Reload Config** | Change parameters without restart |
| **Position-Aware** | Detects existing state on startup |

## Default Strategy

- **Pair**: SOLUSDTPERP (easily changed to BTC, ETH, etc.)
- **Leverage**: 5x
- **Capital**: 90% of available USDT
- **Grid**: 3 levels per side (6 orders)
- **Spacing**: 0.5% between levels (±1.5% total range)
- **Stop-Loss**: 3% below average entry

## Config Examples

See [README.md](../README.md#configuration-examples) for detailed configs with risk profiles.

### Quick Reference

| Profile | Leverage | Levels | Spacing | SL | Expected DD |
|---|---|---|---|---|---|
| Conservative | 3x | 10 | 0.25% | 3% | <15% |
| Balanced | 5x | 5 | 0.5% | 4% | 15–25% |
| Aggressive | 10x | 3 | 1.0% | 6% | 25–40% |

### ⚠️ Validation Rule

```
max_loss_pct > (levels × spacing_pct)
```

Stop-loss must exceed total grid range to avoid premature exits.

## Security

- **Encrypted credentials**: AES-256 via Fernet
- **No plaintext secrets**: Keys stored in `~/.valr-bot/vault.json`
- **Git-safe**: `.gitignore` excludes secrets and build artifacts
- **Subaccount recommended**: Isolate bot from main account

## Support

For issues or questions:
- Check [README.md](../README.md) for detailed docs
- Check [QUICKSTART.md](QUICKSTART.md) for setup help
- VALR API docs: https://api-docs.rooibos.dev/

## Version

**1.0.0** — Released 2026-03-17

---

**Built with ❤️ by Herman De Bot**
