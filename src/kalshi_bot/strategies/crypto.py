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
        self.feed = feed or CryptoFeed(str(self.cfg.get("spot_source", "kraken")), str(self.cfg.get("vol_source", "deribit_dvol")),
                                       int(self.cfg.get("realized_vol_window_hours", 72)),
                                       fallback_realized=bool(self.cfg.get("vol_fallback_realized", True)))
        self.min_minutes = float(self.cfg.get("min_minutes_to_close", 10))
        self.feed_assets = {"BTC", "ETH"}

    def series(self) -> list[str]:
        return list(self.series_map.values())

    def series_patterns(self) -> list[str]:
        """Prefixes per asset, e.g. ``{ BTC = ["KXBTC"], ETH = ["KXETH"] }``. Every open series whose
        ticker starts with one of them is traded as that asset, so the intraday and hourly ladders are
        picked up without anyone hand-copying a ticker out of the Kalshi web app."""
        out: list[str] = []
        for pats in (self.cfg.get("series_patterns") or {}).values():
            out.extend(str(x).upper() for x in (pats if isinstance(pats, (list, tuple)) else [pats]))
        return out

    def wants(self, profile: Any) -> bool:
        asset = self.asset_for_pattern(str(profile.series_ticker))
        return asset is not None and asset in self.feed_assets

    def asset_for_pattern(self, ticker: str) -> str | None:
        t = ticker.upper()
        for asset, pats in (self.cfg.get("series_patterns") or {}).items():
            for pat in (pats if isinstance(pats, (list, tuple)) else [pats]):
                if t.startswith(str(pat).upper()):
                    return str(asset).upper()
        return None

    def adopt_series(self, profiles: list[Any]) -> list[str]:
        added = []
        for p in profiles:
            t = str(p.series_ticker).upper()
            if t in {v.upper() for v in self.series_map.values()}:
                continue
            asset = self.asset_for_pattern(t)
            if asset is None or asset not in self.feed_assets:
                continue
            self.series_map[f"{asset}:{t}"] = t
            added.append(t)
        return added

    async def scan(self, ctx: ScanContext) -> list[Intent]:
        intents: list[Intent] = []
        by_series = {v.upper(): k.split(":", 1)[0] for k, v in self.series_map.items()}
        for event in ctx.events:
            asset = by_series.get((event.series_ticker or "").upper())
            if asset is None:
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
                # the volatility input is chosen per market: a 15-minute ladder and a daily one
                # share an underlying but not a horizon, and 30-day implied vol misprices the former
                try:
                    q = await self.feed.quote(asset, tau_seconds=tau_s)
                except DataUnavailable as e:
                    self.reject("model", f"no spot/vol for {asset}: {e}", m)
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
