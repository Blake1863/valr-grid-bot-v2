#!/usr/bin/env python3
"""Quick dry-run helper. Equivalent to: python3 bot.py --dry-run --pair <PAIR>."""
import os
import sys
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
args = ["python3", os.path.join(ROOT, "bot.py"), "--dry-run"]
if len(sys.argv) > 1:
    args.extend(["--pair", sys.argv[1]])
sys.exit(subprocess.call(args))
