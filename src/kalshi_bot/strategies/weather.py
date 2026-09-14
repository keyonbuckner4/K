"""Weather: daily high/low temperature ladders priced from NWS forecasts and observations.

Model. For a day that has not started, the settlement value is Normal(NWS point forecast, sigma)
with sigma growing with lead time (``sigma_f_day0 + sigma_f_per_day * lead_days``). Once the day
is under way the market knows what has already happened, so the model must too: the high becomes
``max(observed max so far, R)`` where R ~ Normal(max of the remaining hourly forecast, sigma_R)
and sigma_R shrinks from sigma_f_day0 towards ``sigma_f_min`` as the remaining hours run out. The
low is the mirror image with the observed minimum as a cap. Without this the model kept "seeing"
edges against afternoon prices that already reflected the real temperature, which is exactly what
the first observe period's scorecard showed.

Candidates for the current day are only emitted before ``today_candidates_until_local_hour``;
after that the market's live read of the day beats a forecast, so the model still prices every
market for the scorecard but does not propose trades. The NWS grid forecast is deterministic, so
the error model is an assumption stated in config, and the Brier score in ``bot backtest`` is the
check on it. Markets settle on the NWS climatological report for one station: verify the station
in each market's ``rules_primary`` (logged with every decision) against config.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..data.nws import DailyForecast, NWSClient, ObservedExtremes, local_standard_day
from ..errors import DataUnavailable
from ..intent import Intent
from ..models import Event
from ..pricing import prob_yes_normal, to_decimal_prob
from ..risk import ET
from .base import ScanContext, Strategy

log = logging.getLogger(__name__)
_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(?:-|$)")
_MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def event_date(event: Event) -> str | None:
    """Target date of a daily weather event. The ticker (KXHIGHNY-26SEP10) is authoritative:
    Kalshi's strike_date on these events is a timestamp after the day ends, so it names the
    wrong day in UTC. strike_date is only a fallback, taken in Eastern time."""
    m = _DATE.search(event.event_ticker)
    if m:
        yy, mon, dd = m.groups()
        return f"20{yy}-{_MONTHS[mon]:02d}-{int(dd):02d}"
    if event.strike_date is not None:
        return event.strike_date.astimezone(ET).date().isoformat()
    return None


def remaining_forecast_extreme(fc: DailyForecast, kind: str, local_now: datetime) -> tuple[float | None, float]:
    """(extreme of the hourly forecast after ``local_now`` on this date, hours of forecast remaining)."""
    later = [(t, f) for t, f in fc.hourly_f if t > local_now]
    if not later:
        return None, 0.0
    temps = [f for _, f in later]
    return (max(temps) if kind == "high" else min(temps)), float(len(later))


class WeatherStrategy(Strategy):
    name = "weather"

    def __init__(self, cfg: dict[str, Any], storage, nws: NWSClient | None = None):
        super().__init__(cfg, storage)
        self.cities = list(self.cfg.get("cities", []))
        self.sigma0 = float(self.cfg.get("sigma_f_day0", 2.3))
        self.sigma_per_day = float(self.cfg.get("sigma_f_per_day", 0.7))
        self.sigma_min = float(self.cfg.get("sigma_f_min", 1.0))
        self.sigma_full_hours = float(self.cfg.get("sigma_full_after_hours", 8))   # this many forecast hours left = full day-0 sigma
        self.candidates_until_hour = int(self.cfg.get("today_candidates_until_local_hour", 11))
        self.use_observations = bool(self.cfg.get("use_observations", True))
        self.nws = nws or NWSClient(str(self.cfg.get("nws_user_agent", "kalshi-bot")))
        self._series_city: dict[str, tuple[dict[str, Any], str]] = {}
        for c in self.cities:
            if c.get("series_high"):
                self._series_city[str(c["series_high"])] = (c, "high")
            if c.get("series_low"):
                self._series_city[str(c["series_low"])] = (c, "low")

    def series(self) -> list[str]:
        return list(self._series_city)

    def sigma_remaining(self, hours_left: float) -> float:
        frac = min(1.0, max(0.0, hours_left / self.sigma_full_hours)) if self.sigma_full_hours > 0 else 1.0
        return self.sigma_min + (self.sigma0 - self.sigma_min) * frac

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
                tz = await self.nws.timezone(float(city["lat"]), float(city["lon"]))
            except DataUnavailable as e:
                self.reject("model", f"no NWS forecast: {e}", event_ticker=event.event_ticker)
                continue
            fc = daily.get(date)
            point = (fc.high_f if kind == "high" else fc.low_f) if fc else None
            if point is None or fc is None:
                self.reject("model", f"NWS has no {kind} temperature for {date} at {city.get('station')}", event_ticker=event.event_ticker)
                continue
            local_now = ctx.now.astimezone(tz)
            today = local_standard_day(ctx.now, tz)
            lead_days = max(0.0, (datetime.fromisoformat(date + "T12:00:00+00:00") - ctx.now).total_seconds() / 86400)
            mean, sigma, floor, cap = point, self.sigma0 + self.sigma_per_day * lead_days, None, None
            obs: ObservedExtremes | None = None
            hours_left = None
            allow_candidates = True
            if date <= today:
                # the day is under way (or over): bound the model by what has been observed
                if self.use_observations:
                    try:
                        obs = await self.nws.observed_extremes(str(city.get("station")), date, tz)
                    except DataUnavailable as e:
                        log.warning("no observations for %s: %s", city.get("station"), e)
                observed = (obs.max_f if kind == "high" else obs.min_f) if obs else None
                if observed is not None:
                    observed = float(round(observed))   # the climate report settles in whole degrees
                rem, hours_left = remaining_forecast_extreme(fc, kind, local_now) if date == today else (None, 0.0)
                if rem is not None and observed is not None and ((kind == "high" and rem <= observed) or (kind == "low" and rem >= observed)):
                    # the rest of the day is not forecast to beat what has been observed: only reporting noise is left
                    # (hourly observations understate the report's 1-minute extreme by about a degree)
                    mean, sigma = observed, self.sigma_min
                elif rem is not None:
                    # hours of the day still ahead: forecast their extreme, with less uncertainty the fewer they are
                    mean, sigma = rem, self.sigma_remaining(hours_left)
                elif observed is not None and (date < today or local_now.hour >= 20):
                    # the day is over: what was observed is the answer, up to reporting differences (sigma_f_min)
                    mean, sigma = observed, self.sigma_min
                elif observed is not None:
                    # no hourly forecast for the rest of the day (unusual): keep the daily point and full day-0 uncertainty
                    mean, sigma = (max(point, observed) if kind == "high" else min(point, observed)), self.sigma0
                else:
                    mean, sigma = point, self.sigma0
                if observed is not None:
                    if kind == "high":
                        floor = observed
                    else:
                        cap = observed
                allow_candidates = date == today and local_now.hour < self.candidates_until_hour
            self.storage.log_forecast(self.name, f"{event.series_ticker}:{date}", {
                "point_f": point, "mean_f": mean, "sigma_f": sigma, "lead_days": lead_days, "floor_f": floor, "cap_f": cap,
                "hours_left": hours_left, "obs_n": obs.n_obs if obs else None, "obs_latest": obs.latest.isoformat() if obs and obs.latest else None,
                "station": city.get("station"), "issued": fc.issued.isoformat() if fc.issued else None, "candidates_allowed": allow_candidates})
            bound = f" floor {floor:.0f}F" if floor is not None else (f" cap {cap:.0f}F" if cap is not None else "")
            for m in event.markets:
                if not m.is_open():
                    continue
                p = prob_yes_normal(m, mean, sigma, integer_settlement=True, floor=floor, cap=cap)
                if p is None:
                    self.reject("model", f"unpriceable strike_type {m.strike_type!r}", m)
                    continue
                reason = (f"NWS {kind} {mean:.1f}F sigma {sigma:.1f}{bound} for {date} at {city.get('station')}"
                          f" (rules: {(m.rules_primary or '')[:80]!r})")
                it = self.directional_candidate(m, ctx.book(m.ticker), to_decimal_prob(p), ctx, reason)
                if it and not allow_candidates:
                    self.reject("window", f"candidate {it.legs[0].book_side} @ {it.legs[0].price} suppressed: after {self.candidates_until_hour}:00 "
                                          f"local the market's live read of the day beats a forecast", m, model_prob=to_decimal_prob(p),
                                book_side=it.legs[0].book_side, price=it.legs[0].price, edge_net_cents=it.expected_edge_cents)
                    continue
                if it:
                    intents.append(it)
        return intents
