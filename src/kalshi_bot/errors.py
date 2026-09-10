"""Exception types. Every failure the bot can name lives here so callers can be explicit."""

from __future__ import annotations

from typing import Any


class BotError(Exception):
    """Base class for all bot-raised errors."""


class ConfigError(BotError):
    """Missing or contradictory configuration. The bot refuses to start rather than guess."""


class Halted(BotError):
    """Order placement is blocked (HALT file, loss halt, drawdown stop, or exchange closed)."""


class RiskRejected(BotError):
    """RiskEngine refused an order. The message is the reason and is always logged."""


class UnexpectedApiResponse(BotError):
    """The API returned something the docs don't describe. Stop and report; do not guess."""

    def __init__(self, what: str, payload: Any = None):
        self.what = what
        self.payload = payload
        super().__init__(f"{what}: {payload!r}"[:2000])


class ApiError(BotError):
    """Non-2xx response from Kalshi."""

    def __init__(self, status: int, method: str, path: str, body: Any):
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        code = message = None
        if isinstance(body, dict):
            err = body.get("error") if isinstance(body.get("error"), dict) else body
            code = err.get("code")
            message = err.get("message")
        self.code = code
        self.message = message
        super().__init__(f"HTTP {status} {method} {path}: code={code} message={message} body={str(body)[:500]}")


class RateLimited(ApiError):
    """HTTP 429. Kalshi sends no Retry-After header; the client backs off with jitter."""


class DataUnavailable(BotError):
    """A model input (forecast, spot price, vol) could not be fetched. No placeholder is ever substituted."""
