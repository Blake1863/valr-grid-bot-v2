# VALR Grid Bot Fix Summary — 2026-03-18

## Problem
The VALR grid bot was reported to be "crashing and not restarting properly".

## Investigation Findings

### 1. Initial GROUP Errors (Mar 17 14:00)
**Symptom:** Service failed repeatedly with `status=216/GROUP`
```
valr-grid-bot.service: Failed to determine supplementary groups: Operation not permitted
valr-grid-bot.service: Main process exited, code=exited, status=216/GROUP
```

**Root Cause:** systemd user service trying to look up supplementary groups in a restricted environment.

**Fix:** Added `SupplementaryGroups=` (empty) to the service file to prevent group lookups.

### 2. Stale PID File
**Symptom:** PID file contained `237791` but actual running process was `433361`

**Root Cause:** PID file not updated after service restarts

**Fix:** Updated PID file with correct PID after each restart.

### 3. Outdated Binary Running
**Symptom:** Bot showing "Grid not yet placed — skipping re-centre" repeatedly

**Root Cause:** Source code had been updated (Mar 18 03:50) but binary was from earlier build (Mar 18 00:49). The old binary had incomplete grid placement logic.

**Fix:** Rebuilt the binary with `cargo clean && cargo build --release` and restarted the service.

### 4. Logging Configuration
**Issue:** Logs were being appended to files, making rotation and journalctl integration difficult.

**Fix:** Changed service to use `StandardOutput=journal` and `StandardError=journal` for proper systemd journal integration.

## Changes Made

### `/etc/systemd/user/valr-grid-bot.service`
```diff
 [Service]
 Type=simple
 WorkingDirectory=/home/admin/.openclaw/workspace/bots/valr-grid-bot
 ExecStart=/home/admin/.openclaw/workspace/bots/valr-grid-bot/target/release/valr-grid-bot
 Restart=always
 RestartSec=10
 TimeoutStopSec=30
 TimeoutStartSec=30
 SuccessExitStatus=0 143
 KillMode=mixed
 KillSignal=SIGTERM
+# Prevent GROUP errors in restricted environments
+SupplementaryGroups=
+# Log rotation friendly - use journal instead of file append
-StandardOutput=append:/home/admin/.openclaw/workspace/bots/valr-grid-bot/logs/grid-bot.stdout
-StandardError=append:/home/admin/.openclaw/workspace/bots/valr-grid-bot/logs/grid-bot.stderr
+StandardOutput=journal
+StandardError=journal
+SyslogIdentifier=valr-grid-bot
 Environment=RUST_LOG=info
 Environment=HOME=/home/admin
 Environment=PATH=...
```

### Binary Rebuild
```bash
cd /home/admin/.openclaw/workspace/bots/valr-grid-bot
cargo clean
cargo build --release
systemctl --user restart valr-grid-bot.service
```

### PID File Update
```bash
echo "492369" > /home/admin/.openclaw/workspace/bots/valr-grid-bot/logs/grid-bot.pid
```

## Verification

### Service Status (Post-Fix)
```
● valr-grid-bot.service - VALR Grid Bot (SOLUSDTPERP)
     Active: active (running) since Wed 2026-03-18 07:26:08 CST
   Main PID: 492369 (valr-grid-bot)
     Memory: 12.6M
     CPU: 661ms
```

### Bot Functionality
- ✅ Successfully placed 6 grid orders (3 buy + 3 sell)
- ✅ Health checks firing every 5 minutes
- ✅ WebSocket connections stable (auto-reconnect on disconnect)
- ✅ No GROUP errors
- ✅ Logging to systemd journal

### Grid Configuration
- Pair: SOLUSDTPERP
- Leverage: 5x
- Levels: 3 per side (6 total)
- Spacing: 0.4%
- Balance usage: 90%
- Max loss: 3%

### Orders Placed
```
LIVE BUY  @ 94.65
LIVE BUY  @ 94.27
LIVE BUY  @ 93.89
LIVE SELL @ 95.42
LIVE SELL @ 95.80
LIVE SELL @ 96.18
```

## Lessons Learned

1. **Always verify binary timestamps after builds** — As noted in AGENTS.md, old binaries can keep running if copies/rebuilds fail silently.

2. **GROUP errors in user services** — Add `SupplementaryGroups=` to prevent systemd from trying to look up groups in restricted environments.

3. **Use journal for logging** — File append logging is harder to manage than systemd journal integration.

4. **Check source vs binary timestamps** — If source is newer than binary, the running code is stale.

## Monitoring Commands

```bash
# Check service status
systemctl --user status valr-grid-bot.service --no-pager

# View live logs
journalctl --user -u valr-grid-bot.service -f

# Check restart count
systemctl --user show valr-grid-bot.service | grep -E "(NRestarts|FailureCount)"

# Verify binary timestamp
stat /home/admin/.openclaw/workspace/bots/valr-grid-bot/target/release/valr-grid-bot | grep Modify
```

---
**Status:** ✅ FIXED — Bot running stably with proper grid orders placed.
**Next Check:** Monitor for 24h to ensure no crashes or restart issues.
