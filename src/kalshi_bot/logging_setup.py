"""Logging: human-readable console lines plus a rotating file under data/."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


def setup_logging(root: Path, env: str, level: str = "INFO", quiet: bool = False) -> logging.Logger:
    log_dir = root / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    fh = logging.handlers.RotatingFileHandler(log_dir / f"bot.{env}.log", maxBytes=20_000_000, backupCount=10, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if not quiet:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    return logging.getLogger("kalshi_bot")
