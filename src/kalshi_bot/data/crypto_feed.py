"""Spot and volatility inputs for the crypto threshold model. Public endpoints, no keys.

* Kraken ``/0/public/Ticker`` for spot and ``/0/public/OHLC`` (1-minute candles) for realized vol.
* Deribit ``/api/v2/public/get_index_price`` with ``btcdvol_usdc`` / ``ethdvol_usdc`` for the
  DVOL implied-volatility index (annualized, in percent).
Kalshi settles BTC/ETH markets on CF Benchmarks reference rates; Kraken spot is a close proxy,
and any basis is logged with the decision so it can be reviewed.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

import httpx

from ..errors import DataUnavailable

log = logging.getLogger(__name__)
KRAKEN = "https://api.kraken.com"
DERIBIT = "https://www.deribit.com"
PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD"}
MINUTES_PER_YEAR = 365 * 24 * 60
KRAKEN_MAX_CANDLES = 720                      # Kraken returns at most 720 OHLC rows per request
KRAKEN_INTERVALS = (1, 5, 15, 30, 60, 240, 1440, 10080, 21600)  # supported candle sizes in minutes


def kraken_interval_for(window_hours: float) -> int:
    """Smallest supported candle size that fits ``window_hours`` into one 720-row response."""
    need = window_hours * 60 / KRAKEN_MAX_CANDLES
    for iv in KRAKEN_INTERVALS:
        if iv >= need:
            return iv
    return KRAKEN_INTERVALS[-1]


def realized_vol_annualized(closes: list[float], interval_minutes: int) -> float:
    if len(closes) < 30:
        raise DataUnavailable(f"need at least 30 closes for realized vol, got {len(closes)}")
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(MINUTES_PER_YEAR / interval_minutes)


@dataclass
class VolQuote:
    spot: float
    sigma_annual: float
    source: str
    ts: float


class CryptoFeed:
    def __init__(self, spot_source: str = "kraken", vol_source: str = "realized", window_hours: int = 72,
                 transport: httpx.AsyncBaseTransport | None = None, cache_ttl: float = 60.0):
        self.spot_source = spot_source
        self.vol_source = vol_source
        self.window_hours = int(window_hours)
        self._http = httpx.AsyncClient(timeout=15.0, transport=transport, headers={"User-Agent": "kalshi-bot"})
        self._cache: dict[str, VolQuote] = {}
        self.cache_ttl = cache_ttl

    async def close(self) -> None:
        await self._http.aclose()

    async def _json(self, url: str, params: dict | None = None) -> dict:
        try:
            r = await self._http.get(url, params=params)
        except httpx.HTTPError as e:
            raise DataUnavailable(f"{url}: {e}") from e
        if r.status_code != 200:
            raise DataUnavailable(f"{url} returned {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise DataUnavailable(f"{url} returned non-JSON") from e

    async def kraken_spot(self, asset: str) -> float:
        data = await self._json(f"{KRAKEN}/0/public/Ticker", {"pair": PAIRS[asset]})
        if data.get("error"):
            raise DataUnavailable(f"kraken ticker error: {data['error']}")
        result = data.get("result") or {}
        if not result:
            raise DataUnavailable("kraken ticker without result")
        entry = next(iter(result.values()))
        return float(entry["c"][0])

    async def kraken_realized_vol(self, asset: str) -> tuple[float, int, float]:
        """Annualized realized vol over the configured window. Returns (sigma, candle_minutes, hours_covered)."""
        interval = kraken_interval_for(self.window_hours)
        since = int(time.time()) - self.window_hours * 3600
        data = await self._json(f"{KRAKEN}/0/public/OHLC", {"pair": PAIRS[asset], "interval": interval, "since": since})
        if data.get("error"):
            raise DataUnavailable(f"kraken ohlc error: {data['error']}")
        result = data.get("result") or {}
        candles = next((v for k, v in result.items() if k != "last"), None)
        if not candles:
            raise DataUnavailable("kraken ohlc without candles")
        closes = [float(c[4]) for c in candles]
        hours = len(closes) * interval / 60
        if hours < self.window_hours * 0.5:
            raise DataUnavailable(f"kraken returned {len(closes)} x {interval}m candles ({hours:.1f}h), under half the {self.window_hours}h window")
        return realized_vol_annualized(closes, interval), interval, hours

    async def deribit_dvol(self, asset: str) -> float:
        data = await self._json(f"{DERIBIT}/api/v2/public/get_index_price", {"index_name": f"{asset.lower()}dvol_usdc"})
        res = data.get("result") or {}
        if "index_price" not in res:
            raise DataUnavailable("deribit dvol without index_price")
        return float(res["index_price"]) / 100.0

    async def quote(self, asset: str) -> VolQuote:
        asset = asset.upper()
        if asset not in PAIRS:
            raise DataUnavailable(f"unsupported asset {asset}")
        c = self._cache.get(asset)
        if c and time.time() - c.ts < self.cache_ttl:
            return c
        spot = await self.kraken_spot(asset)
        if self.vol_source == "deribit_dvol":
            sigma, src = await self.deribit_dvol(asset), "kraken_spot+deribit_dvol"
        else:
            sigma, interval, hours = await self.kraken_realized_vol(asset)
            src = f"kraken_spot+realized_{hours:.0f}h@{interval}m"
        q = VolQuote(spot, sigma, src, time.time())
        self._cache[asset] = q
        return q
