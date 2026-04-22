# HEARTBEAT.md

Rotate through these. Only alert if something needs attention.

## 🪵 Log sanity (weekly-ish, on Mondays)

Run once:

```bash
find /home/admin/.openclaw/workspace/bots -type f -name '*.log' -size +1G 2>/dev/null
systemctl --user is-active cm-bot-logrotate.timer
systemctl --user list-timers cm-bot-logrotate.timer --no-pager
du -sh /home/admin/.openclaw/workspace/.log-quarantine 2>/dev/null
```

**Alert if:**
- Any single log > 1GB (logrotate trouble or service mis-writing)
- `cm-bot-logrotate.timer` inactive
- Last successful rotation > 2 days ago
- Quarantine > 2GB (auto-purge failing)

Otherwise: `HEARTBEAT_OK`.

## 🤖 Grid bot v3 health (daily)

```bash
systemctl --user is-active valr-grid-bot-v3.service valr-grid-bot-v3-eth.service
tail -20 /home/admin/.openclaw/workspace/bots/valr-grid-bot-v3/logs/bot.log | grep -c 'Insufficient Balance' || true
```

**Alert if:**
- Either service not active
- More than ~10 "Insufficient Balance" errors in last 20 log lines (chronic issue)

## 🔁 CMS1/CMS2 wash bot health (daily)

```bash
systemctl --user is-active cm-bot-spot.service cm-bot-spot-monitor.service
tail -100 /home/admin/.openclaw/workspace/bots/cm-bot-spot/logs/monitor.log
```

**Alert if:**
- Either service not active
- Monitor log shows repeated failed replenishments
