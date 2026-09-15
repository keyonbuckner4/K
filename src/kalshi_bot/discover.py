"""What is actually listed on Kalshi right now, grouped by series.

Series tickers cannot be guessed. The daily Bitcoin ladder is ``KXBTCD``, but the name of the
15-minute one, the oil one or a given economic release is not something to invent, and CLAUDE.md is
explicit that the bot does not guess at the API. So discovery walks the open events through the
``/events`` endpoint the scanner already uses, groups them by series, and reports the horizon and
ladder shape of each. That turns "what is the ticker for 15-minute crypto" into something the
exchange answers rather than something a config file asserts.

The horizon is what separates the market families that share an underlying: a series whose markets
close in minutes is the intraday ladder, one that closes in hours is hourly, one in days is daily.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

from .models import Event

log = logging.getLogger(__name__)


@dataclass
class SeriesProfile:
    series_ticker: str
    category: str | None = None
    events: int = 0
    markets: int = 0
    min_minutes_to_close: float | None = None
    median_minutes_to_close: float | None = None
    strike_types: list[str] = field(default_factory=list)
    mutually_exclusive: bool = False
    sample_event: str = ""
    sample_title: str = ""
    used_by: list[str] = field(default_factory=list)

    @property
    def horizon(self) -> str:
        """The family this series belongs to, from how soon its markets actually close."""
        m = self.median_minutes_to_close
        if m is None:
            return "unknown"
        if m <= 30:
            return "intraday (<=30 min)"
        if m <= 120:
            return "hourly"
        if m <= 36 * 60:
            return "daily"
        if m <= 10 * 24 * 60:
            return "multi-day"
        return "long-dated"

    def to_dict(self) -> dict[str, Any]:
        return {"series": self.series_ticker, "category": self.category, "horizon": self.horizon,
                "events": self.events, "markets": self.markets,
                "minutes_to_close": {"min": round(self.min_minutes_to_close, 1) if self.min_minutes_to_close is not None else None,
                                     "median": round(self.median_minutes_to_close, 1) if self.median_minutes_to_close is not None else None},
                "strike_types": self.strike_types, "ladder": self.mutually_exclusive,
                "sample": {"event": self.sample_event, "title": self.sample_title},
                "used_by": self.used_by or ["(not configured)"]}


def configured_series(toml: Mapping[str, Any]) -> dict[str, list[str]]:
    """Every series ticker the config already points a strategy at, and which strategies those are."""
    out: dict[str, list[str]] = {}

    def add(ticker: Any, strategy: str) -> None:
        t = str(ticker or "").strip().upper()
        if t:
            out.setdefault(t, []).append(strategy)

    strategies = (toml.get("strategies") or {})
    for t in (strategies.get("ladder_arb") or {}).get("series", []) or []:
        add(t, "ladder_arb")
    for city in (strategies.get("weather") or {}).get("cities", []) or []:
        add(city.get("series_high"), "weather")
        add(city.get("series_low"), "weather")
    for t in ((strategies.get("crypto") or {}).get("series") or {}).values():
        add(t, "crypto")
    for t in ((strategies.get("threshold") or {}).get("series") or {}).values():
        add(t, "threshold")
    return out


def profile_series(events: Iterable[Event], now: datetime, configured: Mapping[str, list[str]] | None = None) -> list[SeriesProfile]:
    """Group open events into one profile per series, largest first."""
    configured = configured or {}
    by: dict[str, SeriesProfile] = {}
    closes: dict[str, list[float]] = {}
    strikes: dict[str, set[str]] = {}
    for ev in events:
        key = (ev.series_ticker or "").upper()
        if not key:
            continue
        p = by.get(key)
        if p is None:
            p = by[key] = SeriesProfile(key, used_by=list(configured.get(key, [])))
            closes[key] = []
            strikes[key] = set()
        p.events += 1
        if ev.category and not p.category:
            p.category = ev.category
        if ev.mutually_exclusive:
            p.mutually_exclusive = True
        if not p.sample_event:
            p.sample_event, p.sample_title = ev.event_ticker, str(ev.title or "")
        for m in ev.markets:
            if not m.is_open():
                continue
            p.markets += 1
            if m.strike_type:
                strikes[key].add(str(m.strike_type))
            st = m.settle_time
            if st is not None:
                closes[key].append((st - now).total_seconds() / 60)
    for key, p in by.items():
        future = sorted(c for c in closes[key] if c > 0)
        if future:
            p.min_minutes_to_close = future[0]
            p.median_minutes_to_close = statistics.median(future)
        p.strike_types = sorted(strikes[key])
    return sorted(by.values(), key=lambda p: (-p.markets, p.series_ticker))
