"""Alerts to a webhook (Slack/Discord-style ``{"text": ...}`` JSON). Always logged too."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)
LEVELS = {"info": 0, "warning": 1, "critical": 2}


@dataclass
class Alert:
    level: str
    title: str
    message: str
    ts: float


class Alerts:
    def __init__(self, webhook_url: str | None, min_level: str = "warning", env: str = "demo", transport=None):
        self.webhook_url = webhook_url
        self.min_level = LEVELS.get(min_level, 1)
        self.env = env
        self._queue: list[Alert] = []
        self._last_sent: dict[str, float] = {}
        self._http = httpx.AsyncClient(timeout=5.0, transport=transport)
        self.sent: list[Alert] = []

    def queue(self, level: str, title: str, message: str = "") -> None:
        """Synchronous entry point (RiskEngine is sync). Drained by ``flush``."""
        a = Alert(level, title, message, time.time())
        getattr(log, "critical" if level == "critical" else "warning" if level == "warning" else "info")("ALERT %s: %s %s", level, title, message)
        self._queue.append(a)

    async def send(self, level: str, title: str, message: str = "") -> None:
        self.queue(level, title, message)
        await self.flush()

    async def flush(self) -> None:
        pending, self._queue = self._queue, []
        for a in pending:
            if LEVELS.get(a.level, 0) < self.min_level:
                continue
            key = f"{a.level}:{a.title}"
            if time.time() - self._last_sent.get(key, 0) < 60:
                continue  # de-duplicate bursts
            self._last_sent[key] = time.time()
            self.sent.append(a)
            if not self.webhook_url:
                continue
            text = f"[{self.env}] {a.level.upper()} {a.title}" + (f"\n{a.message}" if a.message else "")
            try:
                await self._http.post(self.webhook_url, json={"text": text, "content": text})
            except httpx.HTTPError as e:
                log.warning("alert webhook failed: %s", e)

    async def close(self) -> None:
        await self._http.aclose()
