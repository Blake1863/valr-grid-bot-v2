# MEMORY.md - Long-Term Memory

## Grid Bot Deployment Architecture (2026-04-07)

### ⚠️ CRITICAL: Account Structure

**SOLUSDTPERP bot does NOT run on the main account.**

Both grid bots run on **subaccounts**:

| Bot | Subaccount | ID |
|---|---|---|
| **SOLUSDTPERP** | Grid Bot 1 | `1432690254033137664` |
| **ETHUSDTPERP** | Grid Bot 2 | `1491067064373735424` |

### Why This Matters

1. **Futures must be enabled per-subaccount** — cannot assume main account status applies
2. **API key scoping** — primary API key can impersonate subaccounts via `X-VALR-SUB-ACCOUNT-ID` header
3. **Balance isolation** — each bot's capital is segregated to its subaccount
4. **Risk containment** — liquidation on one bot doesn't affect the other

### Current Configuration (Both Bots)

- **Leverage:** 10x
- **Levels:** 3 per side (6 total)
- **Spacing:** 0.4% (40 bps)
- **Dynamic Sizing:** ✅ Enabled (auto-adjusts with balance)
- **Capital Allocation:** 90%

### Funding State

- **Grid Bot 1 (SOL):** ~35 USDT (from initial deployment)
- **Grid Bot 2 (ETH):** 40 USDT (unlocked from DeFi lending + transferred)

### API Credentials

Primary API key (`CMSpot`) used for both bots via subaccount impersonation:
- Permissions: `View access`, `Trade`, `Internal Transfer`
- No separate API keys needed per subaccount

### Systemd Services

```bash
# SOL bot
systemctl --user status valr-grid-bot-v2.service

# ETH bot
systemctl --user status valr-grid-bot-v2-eth.service
```

Both services auto-restart on failure.

---

*Created: 2026-04-07 — Post-deployment correction*
