"""Weather: daily high/low temperature ladders priced from NWS forecasts.

Model: settlement value ~ Normal(NWS point forecast, sigma) with sigma growing with lead time
(``sigma_f_day0 + sigma_f_per_day * lead_days``). The NWS grid forecast is deterministic, so this
error model is an assumption, stated in config, and the Brier score in ``bot backtest`` is the
check on it. Markets settle on the NWS climatological report for one station: verify the
station in each market's ``rules_primary`` (logged with every decision) against config.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from ..data.nws import NWSClient
from ..errors import DataUnavailable
from ..intent import Intent
from ..models import Event
from ..pricing import prob_yes_normal, to_decimal_prob
from .base import ScanContext, Strategy

log = logging.getLogger(__name__)
_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(?:-|$)")
_MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def event_date(event: Event) -> str | None:
    if event.strike_date is not None:
        return event.strike_date.date().isoformat()
    m = _DATE.search(event.event_ticker)
    if not m:
        return None
    yy, mon, dd = m.groups()
    return f"20{yy}-{_MONTHS[mon]:02d}-{int(dd):02d}"


class WeatherStrategy(Strategy):
    name = "weather"

    def __init__(self, cfg: dict[str, Any], storage, nws: NWSClient | None = None):
        super().__init__(cfg, storage)
        self.cities = list(self.cfg.get("cities", []))
        self.sigma0 = float(self.cfg.get("sigma_f_day0", 2.3))
        self.sigma_per_day = float(self.cfg.get("sigma_f_per_day", 0.7))
        self.nws = nws or NWSClient(str(self.cfg.get("nws_user_agent", "kalshi-bot")))
        self._series_city: dict[str, tuple[dict[str, Any], str]] = {}
        for c in self.cities:
            if c.get("series_high"):
                self._series_city[str(c["series_high"])] = (c, "high")
            if c.get("series_low"):
                self._series_city[str(c["series_low"])] = (c, "low")

    def series(self) -> list[str]:
        return list(self._series_city)

    async def scan(self, ctx: ScanContext) -> list[Intent]:
        intents: list[Intent] = []
        for event in ctx.events:
            entry = self._series_city.get(event.series_ticker)
            if entry is None:
                continue
            city, kind = entry
            date = event_date(event)
            if date is None:
                self.reject("model", "cannot determine target date from event", event_ticker=event.event_ticker)
                continue
            try:
                daily = await self.nws.daily(float(city["lat"]), float(city["lon"]))
            except DataUnavailable as e:
                self.reject("model", f"no NWS forecast: {e}", event_ticker=event.event_ticker)
                continue
            fc = daily.get(date)
            point = (fc.high_f if kind == "high" else fc.low_f) if fc else None
            if point is None:
                self.reject("model", f"NWS has no {kind} temperature for {date} at {city.get('station')}", event_ticker=event.event_ticker)
                continue
            lead_days = max(0.0, (datetime.fromisoformat(date + "T12:00:00+00:00") - ctx.now).total_seconds() / 86400)
            sigma = self.sigma0 + self.sigma_per_day * lead_days
            self.storage.log_forecast(self.name, f"{event.series_ticker}:{date}", {"point_f": point, "sigma_f": sigma, "lead_days": lead_days,
                                                                                    "station": city.get("station"), "issued": fc.issued.isoformat() if fc and fc.issued else None})
            for m in event.markets:
                if not m.is_open():
                    continue
                p = prob_yes_normal(m, point, sigma, integer_settlement=True)
                if p is None:
                    self.reject("model", f"unpriceable strike_type {m.strike_type!r}", m)
                    continue
                reason = f"NWS {kind} {point:.1f}F sigma {sigma:.1f} for {date} at {city.get('station')} (rules: {(m.rules_primary or '')[:80]!r})"
                it = self.directional_candidate(m, ctx.book(m.ticker), to_decimal_prob(p), ctx, reason)
                if it:
                    intents.append(it)
        return intents
