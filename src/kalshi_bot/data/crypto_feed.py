"""Spot price and volatility for the crypto-threshold strategy.

* Spot: Kraken's public ticker (``GET /0/public/Ticker``), no key needed; the last trade price.
* Vol ``realized``: annualized standard deviation of log returns of Kraken OHLC closes over a
  rolling window (config ``realized_vol_window_hours``). Kraken serves at most 720 candles per
  request, so the candle interval is the smallest whose 720 candles cover the window
  (72 h -> 15-minute candles). Annualized with the same 365-day year ``strategies/crypto.py``
  uses for time to expiry.
* Vol ``deribit_dvol``: the latest close of Deribit's DVOL implied-volatility index
  (``public/get_volatility_index_data``), quoted in vol points and divided by 100. This path was
  not exercised against the live endpoint from the build environment; parsing is strict and any
  surprise raises instead of pricing.

Quotes are cached for one scan interval so several events on the same asset share one fetch.
Every failure raises ``DataUnavailable``; no price or vol is ever substituted.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Sequence

import httpx

from ..errors import ConfigError, DataUnavailable

log = logging.getLogger(__name__)

KRAKEN_URL = "https://api.kraken.com"
DERIBIT_URL = "https://www.deribit.com"
KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD"}
KRAKEN_RESULT_KEYS = {"BTC": ("XXBTZUSD", "XBTUSD"), "ETH": ("XETHZUSD", "ETHUSD")}   # Kraken answers with its canonical names
KRAKEN_INTERVALS = (1, 5, 15, 30, 60, 240, 1440)   # minutes
KRAKEN_MAX_CANDLES = 720
MINUTES_PER_YEAR = 365.0 * 24.0 * 60.0
SPOT_SOURCES = ("kraken",)
VOL_SOURCES = ("realized", "deribit_dvol")


def realized_vol_annualized(closes: Sequence[float], interval_minutes: float, min_samples: int = 30) -> float:
    """Annualized close-to-close volatility from evenly spaced closes ``interval_minutes`` apart."""
    if len(closes) < min_samples + 1:
        raise DataUnavailable(f"realized vol needs at least {min_samples + 1} closes, got {len(closes)}")
    if interval_minutes <= 0:
        raise DataUnavailable(f"invalid candle interval {interval_minutes}")
    rets: list[float] = []
    for a, b in zip(closes, closes[1:]):
        if a <= 0 or b <= 0:
            raise DataUnavailable("non-positive close in candle data")
        rets.append(math.log(b / a))
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sigma = math.sqrt(var) * math.sqrt(MINUTES_PER_YEAR / interval_minutes)
    if not math.isfinite(sigma) or sigma <= 0:
        raise DataUnavailable("realized vol is zero or undefined over the window")
    return sigma


def kraken_interval_for(window_hours: float) -> int:
    """Smallest Kraken candle interval whose 720-candle cap still covers the window."""
    need_minutes = window_hours * 60.0
    for iv in KRAKEN_INTERVALS:
        if iv * KRAKEN_MAX_CANDLES >= need_minutes:
            return iv
    return KRAKEN_INTERVALS[-1]


@dataclass(frozen=True)
class Quote:
    asset: str
    spot: float
    sigma_annual: float     # e.g. 0.55 for 55% annualized
    source: str
    ts: float


class CryptoFeed:
    def __init__(self, spot_source: str = "kraken", vol_source: str = "realized", realized_vol_window_hours: int = 72, *,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0, cache_ttl_sec: float = 30.0,
                 kraken_url: str = KRAKEN_URL, deribit_url: str = DERIBIT_URL):
        self.spot_source = str(spot_source).lower()
        self.vol_source = str(vol_source).lower()
        if self.spot_source not in SPOT_SOURCES:
            raise ConfigError(f"strategies.crypto.spot_source must be one of {SPOT_SOURCES}, got {spot_source!r}")
        if self.vol_source not in VOL_SOURCES:
            raise ConfigError(f"strategies.crypto.vol_source must be one of {VOL_SOURCES}, got {vol_source!r}")
        self.window_hours = float(realized_vol_window_hours)
        if self.window_hours <= 0:
            raise ConfigError("strategies.crypto.realized_vol_window_hours must be positive")
        self.cache_ttl = float(cache_ttl_sec)
        self.kraken_url = kraken_url.rstrip("/")
        self.deribit_url = deribit_url.rstrip("/")
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout, headers={"User-Agent": "kalshi-bot", "Accept": "application/json"})
        self._cache: dict[str, Quote] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def quote(self, asset: str) -> Quote:
        asset = str(asset).upper()
        q = self._cache.get(asset)
        if q and time.time() - q.ts < self.cache_ttl:
            return q
        if asset not in KRAKEN_PAIRS:
            raise DataUnavailable(f"no Kraken pair known for {asset} (supported: {sorted(KRAKEN_PAIRS)})")
        spot = await self._kraken_spot(asset)
        if self.vol_source == "realized":
            sigma, note = await self._kraken_realized_vol(asset)
        else:
            sigma, note = await self._deribit_dvol(asset)
        q = Quote(asset, spot, sigma, f"kraken spot, {note}", time.time())
        self._cache[asset] = q
        return q

    # ---- transport ---------------------------------------------------------------------------
    async def _get_json(self, url: str, params: dict[str, Any], what: str) -> Any:
        try:
            r = await self._http.get(url, params=params)
        except httpx.HTTPError as e:
            raise DataUnavailable(f"{what}: {type(e).__name__}: {e}") from e
        if r.status_code != 200:
            raise DataUnavailable(f"{what}: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise DataUnavailable(f"{what}: response is not JSON") from e

    async def _kraken(self, path: str, params: dict[str, Any], asset: str, what: str) -> Any:
        data = await self._get_json(f"{self.kraken_url}{path}", params, what)
        if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
            raise DataUnavailable(f"{what}: response lacks 'result'")
        if data.get("error"):
            raise DataUnavailable(f"{what}: Kraken error {data['error']}")
        result = data["result"]
        for k in KRAKEN_RESULT_KEYS[asset]:
            if k in result:
                return result[k]
        rest = [k for k in result if k != "last"]
        if len(rest) == 1:
            return result[rest[0]]
        raise DataUnavailable(f"{what}: no entry for {asset} among Kraken result keys {sorted(result)}")

    # ---- sources -----------------------------------------------------------------------------
    async def _kraken_spot(self, asset: str) -> float:
        node = await self._kraken("/0/public/Ticker", {"pair": KRAKEN_PAIRS[asset]}, asset, f"Kraken ticker {asset}")
        c = node.get("c") if isinstance(node, dict) else None
        try:
            spot = float(c[0])
        except (TypeError, ValueError, IndexError):
            raise DataUnavailable(f"Kraken ticker {asset}: no last-trade price ('c') in {str(node)[:200]}") from None
        if not math.isfinite(spot) or spot <= 0:
            raise DataUnavailable(f"Kraken ticker {asset}: bad spot {spot}")
        return spot

    async def _kraken_realized_vol(self, asset: str) -> tuple[float, str]:
        iv = kraken_interval_for(self.window_hours)
        since = int(time.time() - self.window_hours * 3600)
        rows = await self._kraken("/0/public/OHLC", {"pair": KRAKEN_PAIRS[asset], "interval": iv, "since": since}, asset, f"Kraken OHLC {asset}")
        if not isinstance(rows, list):
            raise DataUnavailable(f"Kraken OHLC {asset}: candles are not a list")
        closes: list[float] = []
        for row in rows:  # [time, open, high, low, close, vwap, volume, count]
            try:
                closes.append(float(row[4]))
            except (TypeError, ValueError, IndexError):
                raise DataUnavailable(f"Kraken OHLC {asset}: malformed candle {str(row)[:100]}") from None
        sigma = realized_vol_annualized(closes, iv)
        return sigma, f"realized vol {self.window_hours:g}h@{iv}m ({len(closes)} candles)"

    async def _deribit_dvol(self, asset: str) -> tuple[float, str]:
        end_ms = int(time.time() * 1000)
        params = {"currency": asset, "resolution": "60", "start_timestamp": end_ms - 6 * 3600 * 1000, "end_timestamp": end_ms}
        what = f"Deribit DVOL {asset}"
        data = await self._get_json(f"{self.deribit_url}/api/v2/public/get_volatility_index_data", params, what)
        result = data.get("result") if isinstance(data, dict) else None
        rows = result.get("data") if isinstance(result, dict) else None
        if not isinstance(rows, list) or not rows:
            raise DataUnavailable(f"{what}: no index data in response {str(data)[:200]}")
        last = rows[-1]
        try:
            close = float(last[4])  # [timestamp_ms, open, high, low, close]
        except (TypeError, ValueError, IndexError):
            raise DataUnavailable(f"{what}: unexpected row shape {str(last)[:100]}") from None
        if not 1.0 <= close <= 500.0:
            raise DataUnavailable(f"{what}: implausible index value {close}")
        return close / 100.0, f"deribit DVOL {close:.1f}"
