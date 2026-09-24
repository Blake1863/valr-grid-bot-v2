"""Structured logging for valr-cm-spot.

INFO = one line per cycle outcome (or significant event).
DEBUG = everything else, gated by BOT_DEBUG=1.

Logrotate-compatible: writes to stdout + a single rolling file via stdlib
RotatingFileHandler is intentionally NOT used; we let the system logrotate
config handle rotation (copytruncate strategy, see ~/.config/logrotate/bot-logs.conf).
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def setup(log_dir: Path | str, name: str = "valr-cm-spot") -> logging.Logger:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{name}.log"

    debug = os.environ.get("BOT_DEBUG") == "1"
    level = logging.DEBUG if debug else logging.INFO

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    # Reset handlers (in case of reload)
    for h in list(logger.handlers):
        logger.removeHandler(h)

    fmt = logging.Formatter(
        "[%(asctime)s.%(msecs)03d] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File: append mode, single file. Logrotate copytruncates externally.
    fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(level)
    logger.addHandler(fh)

    # stdout for systemd journal
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(level)
    logger.addHandler(sh)

    return logger
