# MEMORY.md - Long-Term Memory (lean index)

Keep this file SHORT — it's injected into every turn. Details go in
`memory/reference/*.md` and daily notes; link them instead of inlining.

## Standing Rules
- **Trades: always MARKET orders.** Never simple buy orders. (Blake 2026-05-19)
- **Never guess VALR endpoints.** Source of truth: `~/.openclaw/workspace/skills/valr-exchange/references/valr-llms-full.txt`. Grep first; if unsure, ASK.
- **Context efficiency:** durable detail → files (`memory/reference/`, daily notes); this file holds only rules + pointers. Work quietly, no filler narration.

## Current State (updated 2026-08-21)
- 🔴 **Wash trading WOUND DOWN (2026-08-19).** All bots stopped+disabled, do not resurrect. Details + API lessons: `memory/reference/valr-ops.md`. MAIN holds ~528 USDT; subs = dust.
- 🧹 **Cron cleanup (2026-08-21):** crontab is now EMPTY. Removed: cm-bot-v2 trio, **bot-health-monitor (it was auto-RESTARTING dead wash bots!)**, bybit monitor (no positions since May), grid-backtester, dead `/tmp/valr-grid-autocommit`. Disabled `cm-drain-eurc-usdc.timer` (was still moving funds 4-hourly). Balance-report + monthly report RETIRED 2026-08-21 (never delivered; user's call). Backups: `backups/crontab-2026-08-21-*.bak`.
- 🔕 **Heartbeat noise:** `suppressToolErrorWarnings` is a PROTECTED config path (agent can't set it — ask user if wanted). Fixed behaviorally instead: HEARTBEAT.md commands all end `|| true` (no failed exec → no warning leak) + ground rule that polls are never user messages.
- ⏸️ **Joint savings bot paused** (`bots/joint-savings-bot/`). Resume: `systemctl --user enable --now joint-savings-bot`. Creds in encrypted vault only.
- 🪵 Log hygiene: unified logrotate (`cm-bot-logrotate.timer`, 7d) + quarantine auto-purge (14d).
- 🩺 Infra (2026-08-19): two-threshold watchdog (dead=3, hung=10 fails), model timeout 90s (`models.providers.modelstudio.timeoutSeconds`), remote bot has /doctor. Details in TOOLS.md.
- 📹 Video projects CLOSED — do not resume.

## Reference Index
- `memory/reference/valr-ops.md` — VALR API lessons, endpoints, sub IDs, wash-bot workflows (historical)
- `memory/YYYY-MM-DD.md` — daily raw notes
- `TOOLS.md` — environment specifics (watchdog, bot, messaging quirks)
