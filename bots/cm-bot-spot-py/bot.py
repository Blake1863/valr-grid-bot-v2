#!/usr/bin/env python3
"""valr-cm-spot — unified Python wash trading bot for CMS1 ↔ CMS2.

Usage:
    python3 bot.py                # live
    python3 bot.py --dry-run      # no orders placed, prints intended cycles
    python3 bot.py --pair XRPZAR  # restrict to a single pair (live or dry)

Env:
    BOT_DEBUG=1     enable DEBUG logs
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

# Allow running as `python3 bot.py` from the bot directory
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import config as cfg_mod
from src import creds as creds_mod
from src.logger import setup as setup_logger
from src.orchestrator import Orchestrator


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Do not place orders")
    ap.add_argument("--pair", help="Restrict to a single pair (overrides config)")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--name", help="Instance/log name (defaults to config global.instance_name)")
    args = ap.parse_args()

    creds = creds_mod.load()
    cfg = cfg_mod.load(args.config)
    instance_name = args.name or cfg.global_.instance_name or "valr-cm-spot"

    log = setup_logger(ROOT / "logs", name=instance_name)
    log.info("%s starting (dry_run=%s config=%s)", instance_name, args.dry_run, args.config)

    if args.pair:
        # Disable all then enable just the requested pair
        for sym, pc in cfg.pairs.items():
            pc.enabled = (sym == args.pair)
        if args.pair not in cfg.pairs:
            log.error("--pair %s not in config", args.pair)
            return 1

    orch = Orchestrator(cfg, creds, log, dry_run=args.dry_run)
    enabled = await orch.setup()
    log.info("enabled pairs (%d): %s", len(enabled), ", ".join(enabled))

    # Signal handlers for clean shutdown
    loop = asyncio.get_running_loop()

    def _shutdown():
        log.info("signal received — shutting down")
        asyncio.create_task(orch.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    try:
        await orch.run(enabled)
    finally:
        await orch.stop()
    log.info("bye")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 0
    sys.exit(rc)
