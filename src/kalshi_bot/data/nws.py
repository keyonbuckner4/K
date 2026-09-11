"""NWS (api.weather.gov) grid forecast client for the weather strategy.

Two unauthenticated calls; NWS requires a ``User-Agent`` that identifies the application:

* ``GET /points/{lat},{lon}`` resolves coordinates (at most 4 decimals) to a forecast grid
  (``gridId``, ``gridX``, ``gridY``) and the grid's IANA time zone. Cached for the process life.
* ``GET /gridpoints/{gridId}/{gridX},{gridY}`` returns the raw forecast layers. Each layer is
  ``{"uom": ..., "values": [{"validTime": "<ISO-8601 start>/<ISO-8601 duration>", "value": ...}]}``.
  Temperatures are in the unit named by ``uom`` (degC unless it says degF); precipitation
  probability is a percentage.

Daily attribution: every period is keyed to the LOCAL calendar date of its midpoint. A daytime
``maxTemperature`` period (about 7am-7pm local) lands on its own date; an overnight
``minTemperature`` period (about 7pm-7am) lands on the morning it ends, which is when the daily
low is normally set. That is the calendar day a Kalshi "high/low temperature on <date>" market
settles on. Local dates need the IANA time zone database; on Windows that is the ``tzdata``
package, and without it this client raises rather than mis-dating a forecast.

Results are cached per coordinate for ``cache_ttl_sec`` so one scan does not hit NWS once per
event. Every failure raises ``DataUnavailable``. No forecast is ever guessed or substituted.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..errors import DataUnavailable

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.weather.gov"
_DURATION = re.compile(
    r"^P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


def parse_duration(text: str) -> timedelta:
    """ISO-8601 duration as NWS writes them (``PT13H``, ``P1DT2H30M``). Years and months are rejected."""
    m = _DURATION.match(text.strip())
    parts = {k: float(v) for k, v in (m.groupdict().items() if m else ()) if v is not None}
    if not m or not parts:
        raise ValueError(f"unsupported ISO-8601 duration {text!r}")
    return timedelta(weeks=parts.get("weeks", 0.0), days=parts.get("days", 0.0), hours=parts.get("hours", 0.0),
                     minutes=parts.get("minutes", 0.0), seconds=parts.get("seconds", 0.0))


def parse_valid_time(text: str) -> tuple[datetime, timedelta]:
    """``"2026-09-10T11:00:00+00:00/PT13H"`` -> (aware start, duration)."""
    start_s, _, dur_s = text.partition("/")
    if not dur_s:
        raise ValueError(f"validTime without a duration: {text!r}")
    start = datetime.fromisoformat(start_s.replace("Z", "+00:00"))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start, parse_duration(dur_s)


def c_to_f(celsius: float) -> float:
    return celsius * 9.0 / 5.0 + 32.0


@dataclass(frozen=True)
class DailyForecast:
    date: str                       # local calendar date, YYYY-MM-DD
    high_f: float | None = None     # forecast daily high, Fahrenheit
    low_f: float | None = None      # forecast daily low, Fahrenheit
    pop: float | None = None        # probability of precipitation, 0..1 (max over the day's periods)
    issued: datetime | None = None  # NWS updateTime of the grid data
    tz: str | None = None           # IANA zone the dates are expressed in


def _temp_f(value: float, uom: str | None) -> float:
    unit = (uom or "wmoUnit:degC").lower()
    if unit.endswith("degc"):
        return c_to_f(value)
    if unit.endswith("degf"):
        return float(value)
    raise DataUnavailable(f"NWS temperature unit {uom!r} not understood")


def _layer(props: dict[str, Any], name: str) -> tuple[list[Any], str | None]:
    node = props.get(name)
    if node is None:
        return [], None
    if not isinstance(node, dict) or not isinstance(node.get("values"), list):
        raise DataUnavailable(f"NWS gridpoint layer {name!r} has an unexpected shape")
    return node["values"], node.get("uom")


def parse_gridpoint(data: Any, tz: ZoneInfo) -> dict[str, DailyForecast]:
    """Turn a raw gridpoint payload into per-local-date forecasts. Missing layers yield None fields."""
    props = data.get("properties") if isinstance(data, dict) else None
    if not isinstance(props, dict):
        raise DataUnavailable("NWS gridpoint response lacks 'properties'")
    issued: datetime | None = None
    if props.get("updateTime"):
        try:
            issued = datetime.fromisoformat(str(props["updateTime"]).replace("Z", "+00:00"))
        except ValueError:
            issued = None
    highs: dict[str, float] = {}
    lows: dict[str, float] = {}
    pops: dict[str, float] = {}
    for name in ("maxTemperature", "minTemperature", "probabilityOfPrecipitation"):
        values, uom = _layer(props, name)
        for item in values:
            if not isinstance(item, dict) or item.get("value") is None or not item.get("validTime"):
                continue  # NWS leaves gaps as null values; a gap is not a forecast
            try:
                start, dur = parse_valid_time(str(item["validTime"]))
                raw = float(item["value"])
            except (TypeError, ValueError) as e:
                raise DataUnavailable(f"NWS {name} entry unreadable ({e}): {str(item)[:120]}") from e
            date = (start + dur / 2).astimezone(tz).date().isoformat()
            if name == "probabilityOfPrecipitation":
                pops[date] = max(pops.get(date, 0.0), raw / 100.0)
            elif name == "maxTemperature":
                f = _temp_f(raw, uom)
                highs[date] = max(highs.get(date, f), f)
            else:
                f = _temp_f(raw, uom)
                lows[date] = min(lows.get(date, f), f)
    dates = sorted(set(highs) | set(lows) | set(pops))
    return {d: DailyForecast(d, highs.get(d), lows.get(d), pops.get(d), issued, tz.key) for d in dates}


class NWSClient:
    def __init__(self, user_agent: str, *, base_url: str = DEFAULT_BASE_URL, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 15.0, cache_ttl_sec: float = 600.0, failure_ttl_sec: float = 60.0, retries: int = 1):
        if not str(user_agent).strip():
            raise ValueError("NWS requires a User-Agent identifying the application (strategies.weather.nws_user_agent)")
        self.base_url = base_url.rstrip("/")
        self.cache_ttl = float(cache_ttl_sec)
        self.failure_ttl = float(failure_ttl_sec)
        self.retries = int(retries)
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout,
                                       headers={"User-Agent": str(user_agent), "Accept": "application/geo+json"})
        self._grid: dict[tuple[str, str], dict[str, Any]] = {}
        self._daily: dict[tuple[str, str], tuple[float, dict[str, DailyForecast]]] = {}
        self._failed: dict[tuple[str, str], tuple[float, str]] = {}

    async def close(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _key(lat: float, lon: float) -> tuple[str, str]:
        return (f"{float(lat):.4f}", f"{float(lon):.4f}")  # NWS accepts at most four decimals

    async def _get_json(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        last = "no attempt made"
        for attempt in range(self.retries + 1):
            try:
                r = await self._http.get(url)
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if 200 <= r.status_code < 300:
                    try:
                        return r.json()
                    except ValueError as e:
                        raise DataUnavailable(f"NWS {path}: response is not JSON ({e})") from e
                last = f"HTTP {r.status_code}"
                if r.status_code < 500 and r.status_code != 429:
                    break  # a 4xx will not improve on retry
            if attempt < self.retries:
                await asyncio.sleep(0.5 * (attempt + 1))
        raise DataUnavailable(f"NWS {path}: {last}")

    async def grid(self, lat: float, lon: float) -> dict[str, Any]:
        """Resolve coordinates to (gridId, gridX, gridY, timeZone); cached for the life of the client."""
        key = self._key(lat, lon)
        g = self._grid.get(key)
        if g is None:
            data = await self._get_json(f"/points/{key[0]},{key[1]}")
            props = data.get("properties") if isinstance(data, dict) else None
            if not isinstance(props, dict) or any(props.get(k) in (None, "") for k in ("gridId", "gridX", "gridY", "timeZone")):
                raise DataUnavailable(f"NWS /points/{key[0]},{key[1]}: response lacks gridId/gridX/gridY/timeZone")
            g = {"gridId": str(props["gridId"]), "gridX": int(props["gridX"]), "gridY": int(props["gridY"]), "timeZone": str(props["timeZone"])}
            self._grid[key] = g
        return g

    @staticmethod
    def _zone(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception as e:  # ZoneInfoNotFoundError: no IANA database (Windows Python without the tzdata package)
            raise DataUnavailable(f"{name} timezone unavailable on this Python ({e}); on Windows install the tzdata package") from e

    async def daily(self, lat: float, lon: float) -> dict[str, DailyForecast]:
        """Forecast keyed by local calendar date for the grid cell containing (lat, lon)."""
        key = self._key(lat, lon)
        now = time.time()
        cached = self._daily.get(key)
        if cached and now - cached[0] < self.cache_ttl:
            return cached[1]
        failed = self._failed.get(key)
        if failed and now - failed[0] < self.failure_ttl:
            raise DataUnavailable(f"{failed[1]} (not retried for {self.failure_ttl:.0f}s)")
        try:
            g = await self.grid(lat, lon)
            tz = self._zone(g["timeZone"])
            data = await self._get_json(f"/gridpoints/{g['gridId']}/{g['gridX']},{g['gridY']}")
            out = parse_gridpoint(data, tz)
        except DataUnavailable as e:
            self._failed[key] = (time.time(), str(e))
            raise
        self._failed.pop(key, None)
        self._daily[key] = (time.time(), out)
        log.debug("NWS %s,%s: %d forecast days (issued %s)", key[0], key[1], len(out), next(iter(out.values())).issued if out else None)
        return out
