"""Crypto thresholds: BTC/ETH above/below/between price levels at a fixed time, priced as barrier
options from spot and volatility under driftless lognormal dynamics."""

from __future__ import annotations

import logging
from typing import Any

from ..data.crypto_feed import CryptoFeed
from ..errors import DataUnavailable
from ..intent import Intent
from ..pricing import prob_yes_lognormal, to_decimal_prob
from .base import ScanContext, Strategy

log = logging.getLogger(__name__)
SECONDS_PER_YEAR = 365.0 * 86400.0


class CryptoThresholdStrategy(Strategy):
    name = "crypto"

    def __init__(self, cfg: dict[str, Any], storage, feed: CryptoFeed | None = None):
        super().__init__(cfg, storage)
        self.series_map = {str(k).upper(): str(v) for k, v in (self.cfg.get("series") or {}).items()}
        self.feed = feed or CryptoFeed(str(self.cfg.get("spot_source", "kraken")), str(self.cfg.get("vol_source", "realized")),
                                       int(self.cfg.get("realized_vol_window_hours", 72)))
        self.min_minutes = float(self.cfg.get("min_minutes_to_close", 20))

    def series(self) -> list[str]:
        return list(self.series_map.values())

    async def scan(self, ctx: ScanContext) -> list[Intent]:
        intents: list[Intent] = []
        by_series = {v: k for k, v in self.series_map.items()}
        for event in ctx.events:
            asset = by_series.get(event.series_ticker)
            if asset is None:
                continue
            try:
                q = await self.feed.quote(asset)
            except DataUnavailable as e:
                self.reject("model", f"no spot/vol for {asset}: {e}", event_ticker=event.event_ticker)
                continue
            for m in event.markets:
                if not m.is_open():
                    continue
                st = m.settle_time
                if st is None:
                    self.reject("model", "no close time", m)
                    continue
                tau_s = (st - ctx.now).total_seconds()
                if tau_s < self.min_minutes * 60:
                    self.reject("model", f"closes in {tau_s / 60:.1f} min < {self.min_minutes}", m)
                    continue
                p = prob_yes_lognormal(m, q.spot, q.sigma_annual, tau_s / SECONDS_PER_YEAR)
                if p is None:
                    self.reject("model", f"unpriceable strike_type {m.strike_type!r}", m)
                    continue
                self.storage.log_forecast(self.name, f"{event.series_ticker}:{m.ticker}", {"spot": q.spot, "sigma": q.sigma_annual, "source": q.source,
                                                                                            "tau_minutes": tau_s / 60, "p_yes": p})
                reason = f"{asset} spot {q.spot:.2f} sigma {q.sigma_annual:.2%} ({q.source}) tau {tau_s / 3600:.2f}h"
                it = self.directional_candidate(m, ctx.book(m.ticker), to_decimal_prob(p), ctx, reason)
                if it:
                    intents.append(it)
        return intents
