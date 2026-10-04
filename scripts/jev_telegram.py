#!/usr/bin/env python3
"""Telegram alerting — stdlib only, fail-open. NEVER raises on send failure.

Token/chat_id read from repo-root .env (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)
via load_telegram_config(), or passed straight to TelegramAlerter(...).
Neither value is ever printed or logged.

Missing chat_id (not yet captured via getUpdates) or network/API failure both
degrade to a no-op send: alerting must never be able to take down the
supervisor loop it is reporting on.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def load_telegram_config(env_path: Optional[Path] = None) -> tuple[Optional[str], Optional[str]]:
    """Return (bot_token, chat_id) from repo-root .env; either may be None."""
    path = Path(env_path) if env_path else ROOT / ".env"
    token = None
    chat_id = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            token = line.split("=", 1)[1].strip().strip('"').strip("'") or None
        elif line.startswith("TELEGRAM_CHAT_ID="):
            chat_id = line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return token, chat_id


class TelegramAlerter:
    """Minimal sendMessage client. Fail-open: every failure yields ok=False."""

    def __init__(self, bot_token: Optional[str], chat_id: Optional[str],
                 timeout: float = 5.0) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self.timeout = float(timeout)
        self.sent = 0
        self.errors = 0

    @property
    def configured(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    def send(self, text: str) -> bool:
        """Best-effort send; returns True on a 200 response, False otherwise."""
        if not self.configured:
            return False
        url = API_BASE.format(token=self._bot_token)
        payload = json.dumps({"chat_id": self._chat_id, "text": text}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ok = 200 <= resp.status < 300
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
            ok = False
        if ok:
            self.sent += 1
        else:
            self.errors += 1
        return ok


def from_env(env_path: Optional[Path] = None, timeout: float = 5.0) -> TelegramAlerter:
    token, chat_id = load_telegram_config(env_path)
    return TelegramAlerter(token, chat_id, timeout=timeout)
