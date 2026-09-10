"""Strategies emit Intents; they never touch the exchange."""

from __future__ import annotations

from typing import Any

from ..storage import Storage
from .base import Strategy


def build_strategies(cfg: dict[str, Any], storage: Storage, **feeds: Any) -> list[Strategy]:
    from .crypto import CryptoThresholdStrategy
    from .economics import EconomicsStrategy
    from .ladder_arb import LadderArbStrategy
    from .weather import WeatherStrategy

    scfg = cfg.get("strategies", {})
    out: list[Strategy] = []
    if scfg.get("ladder_arb", {}).get("enabled", False):
        out.append(LadderArbStrategy(scfg["ladder_arb"], storage))
    if scfg.get("weather", {}).get("enabled", False):
        out.append(WeatherStrategy(scfg["weather"], storage, nws=feeds.get("nws")))
    if scfg.get("crypto", {}).get("enabled", False):
        out.append(CryptoThresholdStrategy(scfg["crypto"], storage, feed=feeds.get("crypto")))
    if scfg.get("economics", {}).get("enabled", False):
        out.append(EconomicsStrategy(scfg["economics"], storage))
    return out
