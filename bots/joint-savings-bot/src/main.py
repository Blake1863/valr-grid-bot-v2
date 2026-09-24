"""Joint Savings Bot — entry point."""

import json
import sys
import os
import time
import logging
import logging.handlers

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def setup_logging(log_file: str):
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    root = logging.getLogger("joint-savings")
    root.setLevel(logging.INFO)

    # File handler with rotation
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=50_000_000, backupCount=5
    )
    fh.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(ch)

    return root


def load_vault_secret(name: str) -> str:
    """Fetch a secret from the encrypted vault (~/.openclaw/secrets)."""
    import subprocess
    r = subprocess.run(
        [sys.executable, "/home/admin/.openclaw/secrets/secrets.py", "get", name],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"Failed to load secret '{name}' from vault: {r.stderr.strip()}")
    return r.stdout.strip()


def main():
    config_path = os.path.join(BASE_DIR, "config.json")
    with open(config_path, "r") as f:
        config = json.load(f)

    # Credentials live in the encrypted vault, not in config.json
    if not config.get("api_key"):
        config["api_key"] = load_vault_secret("joint_savings_api_key")
    if not config.get("api_secret"):
        config["api_secret"] = load_vault_secret("joint_savings_api_secret")

    log_file = os.path.join(BASE_DIR, config.get("log_file", "logs/bot.log"))
    state_path = os.path.join(BASE_DIR, config.get("state_file", "state.json"))
    poll_interval = config.get("poll_interval_sec", 30)

    log = setup_logging(log_file)
    log.info("=" * 60)
    log.info("Joint Savings Bot starting")
    log.info("Pair: %s | Min buy: R%.2f | Poll: %ds", config["pair"], config.get("min_buy_amount_zar", 10), poll_interval)
    log.info("=" * 60)

    # Import bot after logging is set up
    from .bot import JointSavingsBot
    bot = JointSavingsBot(config, state_path)

    log.info("Polling for ZAR deposits every %d seconds...", poll_interval)

    while True:
        try:
            bought = bot.run_once()
            if bought:
                log.info("✅ Purchase completed. Sleeping %ds.", poll_interval)
            else:
                log.debug("No new deposits. Sleeping %ds.", poll_interval)
        except Exception as e:
            log.error("💥 Unhandled error in poll cycle: %s", e, exc_info=True)

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
