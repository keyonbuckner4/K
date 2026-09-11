"""NOAA / National Weather Service forecasts (api.weather.gov, free, no key; User-Agent required).

``/points/{lat},{lon}`` resolves the forecast office grid, ``/gridpoints/{wfo}/{x},{y}`` returns
the raw numeric forecast: ``maxTemperature`` / ``minTemperature`` in Celsius with ISO-8601
``validTime`` periods, ``probabilityOfPrecipitation`` in percent. The bot converts to Fahrenheit
and keys values by the local calendar date at the station.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..errors import DataUnavailable

log = logging.getLogger(__name__)
BASE = "https://api.weather.gov"
_DUR = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?")


def parse_duration(s: str) -> timedelta:
    m = _DUR.fullmatch(s)
    if not m:
        raise ValueError(f"bad ISO duration {s!r}")
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    return timedelta(days=d, hours=h, minutes=mi)


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


@dataclass
class DailyForecast:
    date: str            # local ISO date at the station
    high_f: float | None
    low_f: float | None
    pop: float | None    # probability of precipitation, 0..1 (max over the day)
    issued: datetime | None


class NWSClient:
    def __init__(self, user_agent: str, transport: httpx.AsyncBaseTransport | None = None, cache_ttl: float = 900.0):
        if not user_agent or "set your email" in user_agent:
            log.warning("NWS asks for a contact in the User-Agent; set strategies.weather.nws_user_agent in config/bot.toml")
        self._http = httpx.AsyncClient(base_url=BASE, timeout=15.0, transport=transport,
                                       headers={"User-Agent": user_agent or "kalshi-bot", "Accept": "application/geo+json"})
        self._points: dict[str, dict[str, Any]] = {}
        self._grid: dict[str, tuple[float, dict[str, DailyForecast]]] = {}
        self.cache_ttl = cache_ttl

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str) -> dict[str, Any]:
        try:
            r = await self._http.get(path)
        except httpx.HTTPError as e:
            raise DataUnavailable(f"NWS request failed: {path}: {e}") from e
        if r.status_code != 200:
            raise DataUnavailable(f"NWS {path} returned {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise DataUnavailable(f"NWS {path} returned non-JSON") from e

    async def points(self, lat: float, lon: float) -> dict[str, Any]:
        key = f"{lat:.4f},{lon:.4f}"
        if key not in self._points:
            data = await self._get(f"/points/{lat:.4f},{lon:.4f}")
            props = data.get("properties") or {}
            if not props.get("gridId") or props.get("gridX") is None:
                raise DataUnavailable(f"NWS /points response without grid for {key}")
            self._points[key] = props
        return self._points[key]

    async def daily(self, lat: float, lon: float) -> dict[str, DailyForecast]:
        key = f"{lat:.4f},{lon:.4f}"
        cached = self._grid.get(key)
        if cached and time.time() - cached[0] < self.cache_ttl:
            return cached[1]
        props = await self.points(lat, lon)
        tz = ZoneInfo(props.get("timeZone") or "America/New_York")
        grid = await self._get(f"/gridpoints/{props['gridId']}/{props['gridX']},{props['gridY']}")
        gp = grid.get("properties") or {}
        issued = gp.get("updateTime")
        issued_dt = datetime.fromisoformat(issued.replace("Z", "+00:00")) if issued else None
        out: dict[str, DailyForecast] = {}

        def _values(name: str) -> list[tuple[datetime, timedelta, float]]:
            node = gp.get(name) or {}
            vals = []
            for v in node.get("values") or []:
                if v.get("value") is None:
                    continue
                start_s, _, dur_s = str(v["validTime"]).partition("/")
                start = datetime.fromisoformat(start_s.replace("Z", "+00:00"))
                vals.append((start, parse_duration(dur_s) if dur_s else timedelta(hours=1), float(v["value"])))
            return vals

        def _ensure(date: str) -> DailyForecast:
            if date not in out:
                out[date] = DailyForecast(date, None, None, None, issued_dt)
            return out[date]

        for start, dur, c in _values("maxTemperature"):
            mid = (start + dur / 2).astimezone(tz)
            fc = _ensure(mid.date().isoformat())
            fc.high_f = c_to_f(c) if fc.high_f is None else max(fc.high_f, c_to_f(c))
        for start, dur, c in _values("minTemperature"):
            end = (start + dur).astimezone(tz)
            fc = _ensure(end.date().isoformat())
            fc.low_f = c_to_f(c) if fc.low_f is None else min(fc.low_f, c_to_f(c))
        for start, dur, pct in _values("probabilityOfPrecipitation"):
            local = start.astimezone(tz)
            fc = _ensure(local.date().isoformat())
            p = pct / 100.0
            fc.pop = p if fc.pop is None else max(fc.pop, p)
        if not out:
            raise DataUnavailable(f"NWS grid for {key} has no temperature values")
        self._grid[key] = (time.time(), out)
        return out
