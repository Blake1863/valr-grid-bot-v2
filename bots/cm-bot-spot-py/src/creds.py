"""Credential loader. Reuses the .env that the existing bots use."""
from __future__ import annotations

from pathlib import Path
from typing import Dict

DEFAULT_ENV_PATH = Path(__file__).resolve().parents[2] / "cm-bot-spot" / ".env"

REQUIRED = ("MAIN_API_KEY", "MAIN_API_SECRET", "CM1_SUBACCOUNT_ID", "CM2_SUBACCOUNT_ID")


def load(env_path: Path | str = DEFAULT_ENV_PATH) -> Dict[str, str]:
    env_path = Path(env_path)
    if not env_path.exists():
        raise FileNotFoundError(f".env not found at {env_path}")
    creds: Dict[str, str] = {}
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip().strip('"').strip("'")
    missing = [k for k in REQUIRED if k not in creds]
    if missing:
        raise RuntimeError(f"Missing credentials in {env_path}: {missing}")
    return creds
