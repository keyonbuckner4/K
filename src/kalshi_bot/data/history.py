"""Historical spot and volatility, for scoring a model against markets that have already settled.

The live feed answers "what is the price now"; a backfill needs "what was the price at 14:00 last
Tuesday", because the only honest way to score a model on old markets is to feed it exactly what it
would have had at the moment it decided.

* Kraken ``/0/public/OHLC`` with ``since`` returns at most 720 rows, so the candle size is chosen to
  cover the requested span in one response (the same cap the live feed works around).
* Deribit ``/api/v2/public/get_volatility_index_data`` returns DVOL candles, the historical form of
  the implied-volatility index the live crypto model uses.

Gaps are never interpolated. ``HistorySeries.at`` refuses to answer when the nearest earlier sample
is older than ``max_staleness_sec``, so a hole in the data becomes a skipped decision point with a
reason, never an invented price.
"""

from __future__ import annotations

import bisect
import logging
import math
from dataclasses import dataclass, field

import httpx

from ..errors import DataUnavailable
from .crypto_feed import KRAKEN, KRAKEN_INTERVALS, KRAKEN_MAX_CANDLES, MINUTES_PER_YEAR, PAIRS, realized_vol_annualized

log = logging.getLogger(__name__)
DERIBIT = "https://www.deribit.com"


def interval_for_span(span_seconds: float) -> int:
    """Smallest supported Kraken candle size that covers ``span_seconds`` in one 720-row response."""
    need_minutes = span_seconds / 60 / KRAKEN_MAX_CANDLES
    for iv in KRAKEN_INTERVALS:
        if iv >= need_minutes:
            return iv
    raise DataUnavailable(f"span of {span_seconds / 86400:.1f} days exceeds what one Kraken response can cover")


@dataclass
class HistorySeries:
    """Timestamped samples, oldest first, answered as-of a point in time without interpolation."""

    name: str
    times: list[float] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    interval_sec: float = 3600.0

    def __len__(self) -> int:
        return len(self.times)

    @property
    def span(self) -> tuple[float, float] | None:
        return (self.times[0], self.times[-1]) if self.times else None

    def at(self, ts: float, max_staleness_sec: float | None = None) -> float:
        """Most recent sample at or before ``ts``. Raises if there is none, or if it is too stale."""
        if not self.times:
            raise DataUnavailable(f"{self.name}: no samples")
        i = bisect.bisect_right(self.times, ts) - 1
        if i < 0:
            raise DataUnavailable(f"{self.name}: no sample at or before {ts:.0f} (earliest is {self.times[0]:.0f})")
        limit = self.interval_sec * 3 if max_staleness_sec is None else max_staleness_sec
        age = ts - self.times[i]
        if age > limit:
            raise DataUnavailable(f"{self.name}: nearest sample is {age / 60:.0f} min old at {ts:.0f} (limit {limit / 60:.0f} min); not interpolating")
        return self.values[i]

    def realized_vol_at(self, ts: float, window_hours: float) -> float:
        """Annualized realized volatility over the ``window_hours`` ending at ``ts``, from these samples."""
        start = ts - window_hours * 3600
        lo = bisect.bisect_left(self.times, start)
        hi = bisect.bisect_right(self.times, ts)
        closes = self.values[lo:hi]
        if len(closes) < 30:
            raise DataUnavailable(f"{self.name}: only {len(closes)} samples in the {window_hours:.0f}h before {ts:.0f}, need 30")
        return realized_vol_annualized(closes, int(self.interval_sec / 60))


class HistoryFeed:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0):
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport, headers={"User-Agent": "kalshi-bot"})
        self._spot: dict[tuple[str, int], HistorySeries] = {}
        self._dvol: dict[tuple[str, int], HistorySeries] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def _json(self, url: str, params: dict) -> dict:
        try:
            r = await self._http.get(url, params=params)
        except httpx.HTTPError as e:
            raise DataUnavailable(f"{url}: {e}") from e
        if r.status_code != 200:
            raise DataUnavailable(f"{url} returned {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise DataUnavailable(f"{url} returned non-JSON") from e

    async def spot_history(self, asset: str, start_ts: float, end_ts: float) -> HistorySeries:
        """Kraken close prices covering [start_ts, end_ts]."""
        asset = asset.upper()
        if asset not in PAIRS:
            raise DataUnavailable(f"unsupported asset {asset}")
        interval = interval_for_span(end_ts - start_ts)
        key = (asset, interval)
        if key in self._spot and self._spot[key].times and self._spot[key].times[0] <= start_ts:
            return self._spot[key]
        data = await self._json(f"{KRAKEN}/0/public/OHLC", {"pair": PAIRS[asset], "interval": interval, "since": int(start_ts)})
        if data.get("error"):
            raise DataUnavailable(f"kraken ohlc error: {data['error']}")
        result = data.get("result") or {}
        rows = next((v for k, v in result.items() if k != "last"), None)
        if not rows:
            raise DataUnavailable("kraken ohlc without candles")
        series = HistorySeries(f"kraken_{asset}_{interval}m", interval_sec=interval * 60.0)
        for row in rows:
            try:
                series.times.append(float(row[0]))
                series.values.append(float(row[4]))
            except (IndexError, TypeError, ValueError) as e:
                raise DataUnavailable(f"kraken ohlc row not [time, o, h, l, close, ...]: {row!r}") from e
        self._spot[key] = series
        log.info("spot history %s: %d candles at %dm covering %.1f days", asset, len(series), interval,
                 (series.times[-1] - series.times[0]) / 86400 if len(series) > 1 else 0)
        return series

    async def dvol_history(self, asset: str, start_ts: float, end_ts: float, resolution_sec: int = 3600) -> HistorySeries:
        """Deribit DVOL implied-volatility index candles, as a fraction (0.55 = 55%)."""
        asset = asset.upper()
        key = (asset, resolution_sec)
        if key in self._dvol and self._dvol[key].times and self._dvol[key].times[0] <= start_ts:
            return self._dvol[key]
        data = await self._json(f"{DERIBIT}/api/v2/public/get_volatility_index_data",
                                {"currency": asset, "start_timestamp": int(start_ts * 1000),
                                 "end_timestamp": int(end_ts * 1000), "resolution": str(resolution_sec)})
        rows = ((data.get("result") or {}).get("data")) if isinstance(data.get("result"), dict) else None
        if not isinstance(rows, list) or not rows:
            raise DataUnavailable(f"deribit volatility index returned no data for {asset}")
        series = HistorySeries(f"dvol_{asset}", interval_sec=float(resolution_sec))
        for row in rows:
            try:
                series.times.append(float(row[0]) / 1000.0)
                series.values.append(float(row[4]) / 100.0)   # [ts_ms, open, high, low, close] in percent
            except (IndexError, TypeError, ValueError) as e:
                raise DataUnavailable(f"deribit volatility row not [ts_ms, o, h, l, close]: {row!r}") from e
        order = sorted(range(len(series.times)), key=lambda i: series.times[i])
        series.times = [series.times[i] for i in order]
        series.values = [series.values[i] for i in order]
        self._dvol[key] = series
        log.info("dvol history %s: %d candles covering %.1f days", asset, len(series),
                 (series.times[-1] - series.times[0]) / 86400 if len(series) > 1 else 0)
        return series
