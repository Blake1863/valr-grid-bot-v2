"""Telegram alerter — sends operator notifications via the bot used by OpenClaw.

Uses Telegram Bot API directly with the bot token that's already in
~/.openclaw/openclaw.json under channels.telegram.botToken. We avoid taking a
hard dependency on OpenClaw internals; this is a small, self-contained sender.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
from pathlib import Path
from typing import Optional

import aiohttp

OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"


def _read_telegram_token() -> Optional[str]:
    try:
        cfg = _json.loads(OPENCLAW_CONFIG.read_text())
    except Exception:
        return None
    try:
        return cfg["channels"]["telegram"]["botToken"]
    except (KeyError, TypeError):
        return None


class TelegramAlerter:
    def __init__(self, chat_id: str, *, bot_token: Optional[str] = None,
                 logger: Optional[logging.Logger] = None):
        self.chat_id = chat_id
        self.token = bot_token or _read_telegram_token()
        self.log = logger or logging.getLogger("alerter")
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "TelegramAlerter":
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def send(self, text: str) -> bool:
        if not self.token or not self.chat_id:
            self.log.warning("alerter: missing token or chat_id, skipping send")
            return False
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        # Strip the "telegram:" prefix if present — the bot API takes raw chat_id.
        chat = self.chat_id
        if chat.startswith("telegram:"):
            chat = chat.split(":", 1)[1]
        payload = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
        try:
            async with self._session.post(url, json=payload) as r:
                if r.status == 200:
                    return True
                body = await r.text()
                self.log.warning("alerter: send failed status=%s body=%s", r.status, body[:200])
                return False
        except Exception as e:
            self.log.warning("alerter: exception %s", e)
            return False
