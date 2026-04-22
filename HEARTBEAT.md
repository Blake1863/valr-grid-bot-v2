# HEARTBEAT.md

Rotate through these. Only alert if something needs attention.

## 🪵 Log sanity (weekly-ish, on Mondays)

```bash
find /home/admin/.openclaw/workspace/bots -type f -name '*.log' -size +1G 2>/dev/null
systemctl --user is-active cm-bot-logrotate.timer
systemctl --user list-timers cm-bot-logrotate.timer --no-pager
du -sh /home/admin/.openclaw/workspace/.log-quarantine 2>/dev/null
```

**Alert if:**
- Any single log > 1GB
- `cm-bot-logrotate.timer` inactive
- Last successful rotation > 2 days ago
- Quarantine > 2GB

## 🤖 Grid bot v4 health (daily) — ONLY IF DEPLOYED

```bash
# Skip this block entirely if the services below don't exist yet
systemctl --user is-active valr-perpetual-grid-bot@sol.service valr-perpetual-grid-bot@eth.service 2>/dev/null
tail -20 /home/admin/.openclaw/workspace/bots/valr-perpetual-grid-bot/logs/sol.log 2>/dev/null | grep -cE 'Insufficient Balance|Stop-loss triggered|HALTED|circuit breaker' || true
```

**Alert if:**
- Either service not active (after first deploy)
- Any stop-loss / circuit-breaker / halted events in recent logs
- More than ~10 "Insufficient Balance" errors in last 20 lines (regression)

**NOT YET DEPLOYED as of 2026-04-22** — awaiting user first launch. Skip this check until services exist.

## 🔁 CMS1/CMS2 wash bot health (daily)

```bash
systemctl --user is-active cm-bot-spot.service cm-bot-spot-monitor.service cm-bot-v2.service
tail -100 /home/admin/.openclaw/workspace/bots/cm-bot-spot/logs/monitor.log
```

**Alert if:**
- Any service not active
- Monitor log shows repeated failed replenishments
