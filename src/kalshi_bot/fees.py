"""Kalshi trading fees.

Schedule (Kalshi fee schedule, 2026; the API exposes the per-series parameters):

* ``fee_type == "quadratic"``: taker fee = ``round_up_to_cent(multiplier * C * P * (1 - P))``
  per order, where C is contracts, P the YES-referenced price in dollars, and ``multiplier``
  is the series' ``fee_multiplier`` (0.07 on the general schedule). Makers pay nothing.
* ``fee_type == "quadratic_with_maker_fees"``: as above, and makers pay the same curve with the
  maker multiplier (0.0175 on the published schedule, one quarter of the taker rate).
* ``fee_type == "flat"``: a per-contract flat fee. Kalshi does not publish it via the series
  object, so it must be configured explicitly or the series is refused.

The fee is largest at 50 cents (1.75c/contract taker on the general schedule) and rounds UP to
the next cent per order, which matters for small orders. Every edge calculation subtracts it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any, Mapping

from .errors import DataUnavailable, UnexpectedApiResponse
from .models import Series

CENT = Decimal("0.01")
QUADRATIC = "quadratic"
QUADRATIC_WITH_MAKER = "quadratic_with_maker_fees"
FLAT = "flat"
KNOWN_FEE_TYPES = (QUADRATIC, QUADRATIC_WITH_MAKER, FLAT)


def quadratic_fee(count: int | Decimal, price: Decimal, multiplier: Decimal) -> Decimal:
    """Fee in dollars for one order, rounded up to the next cent."""
    c = Decimal(count)
    p = Decimal(price)
    if c <= 0:
        return Decimal("0.00")
    if not (Decimal("0") <= p <= Decimal("1")):
        raise ValueError(f"price must be in [0, 1] dollars, got {p}")
    raw = Decimal(multiplier) * c * p * (Decimal("1") - p)
    return raw.quantize(CENT, rounding=ROUND_CEILING)


@dataclass(frozen=True)
class SeriesFee:
    series_ticker: str
    fee_type: str
    taker_multiplier: Decimal
    maker_multiplier: Decimal
    flat_per_contract: Decimal | None
    source: str  # api | config

    def fee(self, count: int | Decimal, price: Decimal, taker: bool = True) -> Decimal:
        if self.fee_type == FLAT:
            assert self.flat_per_contract is not None
            return (self.flat_per_contract * Decimal(count)).quantize(CENT, rounding=ROUND_CEILING)
        mult = self.taker_multiplier if taker else self.maker_multiplier
        return quadratic_fee(count, price, mult)

    def fee_cents_per_contract(self, count: int | Decimal, price: Decimal, taker: bool = True) -> Decimal:
        c = Decimal(count)
        if c <= 0:
            return Decimal("0")
        return self.fee(count, price, taker) * 100 / c


class FeeSchedule:
    """Per-series fee parameters. Populated from the API; config may override or add series."""

    def __init__(self, default_taker_multiplier: Decimal = Decimal("0.07"),
                 maker_multiplier: Decimal = Decimal("0.0175"),
                 series_overrides: Mapping[str, Any] | None = None,
                 flat_overrides: Mapping[str, Any] | None = None):
        self.default_taker_multiplier = Decimal(str(default_taker_multiplier))
        self.maker_multiplier = Decimal(str(maker_multiplier))
        self._series: dict[str, SeriesFee] = {}
        self._overrides: dict[str, Decimal] = {k: Decimal(str(v)) for k, v in (series_overrides or {}).items()}
        self._flat: dict[str, Decimal] = {k: Decimal(str(v)) for k, v in (flat_overrides or {}).items()}

    @classmethod
    def from_config(cls, fees_cfg: Mapping[str, Any] | None) -> "FeeSchedule":
        cfg = fees_cfg or {}
        return cls(
            default_taker_multiplier=Decimal(str(cfg.get("default_multiplier", "0.07"))),
            maker_multiplier=Decimal(str(cfg.get("maker_multiplier", "0.0175"))),
            series_overrides=cfg.get("series_overrides") or {},
            flat_overrides=cfg.get("flat_fee_per_contract") or {},
        )

    def register_series(self, series: Series) -> SeriesFee:
        """Record the fee parameters Kalshi reports for a series. Unknown shapes raise."""
        fee_type = series.fee_type
        if fee_type is None and series.fee_multiplier is None:
            raise UnexpectedApiResponse("series without fee_type/fee_multiplier", series.raw)
        if fee_type not in KNOWN_FEE_TYPES:
            raise UnexpectedApiResponse(f"unknown fee_type {fee_type!r} for series {series.ticker}", series.raw)
        mult = series.fee_multiplier
        if fee_type in (QUADRATIC, QUADRATIC_WITH_MAKER):
            if mult is None or not (Decimal("0") <= mult <= Decimal("1")):
                raise UnexpectedApiResponse(f"fee_multiplier {mult!r} outside [0, 1] for series {series.ticker}", series.raw)
            maker = self.maker_multiplier if fee_type == QUADRATIC_WITH_MAKER else Decimal("0")
            sf = SeriesFee(series.ticker, fee_type, mult, maker, None, "api")
        else:
            flat = self._flat.get(series.ticker)
            if flat is None:
                raise DataUnavailable(
                    f"series {series.ticker} has fee_type 'flat'; set [fees.flat_fee_per_contract] {series.ticker} in config"
                )
            sf = SeriesFee(series.ticker, FLAT, Decimal("0"), Decimal("0"), flat, "config")
        self._series[series.ticker] = sf
        return sf

    def register_override(self, series_ticker: str, taker_multiplier: Decimal, maker: bool = False) -> SeriesFee:
        sf = SeriesFee(series_ticker, QUADRATIC_WITH_MAKER if maker else QUADRATIC, Decimal(str(taker_multiplier)),
                       self.maker_multiplier if maker else Decimal("0"), None, "config")
        self._series[series_ticker] = sf
        return sf

    def known(self, series_ticker: str) -> bool:
        return series_ticker in self._series or series_ticker in self._overrides

    def for_series(self, series_ticker: str) -> SeriesFee:
        """Fee parameters for a series. Raises DataUnavailable rather than guessing."""
        if series_ticker in self._overrides:
            return SeriesFee(series_ticker, QUADRATIC, self._overrides[series_ticker], Decimal("0"), None, "config")
        sf = self._series.get(series_ticker)
        if sf is None:
            raise DataUnavailable(f"no fee parameters for series {series_ticker}; fetch GET /series/{series_ticker} first")
        return sf

    def default_fee(self, count: int | Decimal, price: Decimal) -> Decimal:
        """General-schedule taker fee. For display and observe-only estimates only."""
        return quadratic_fee(count, price, self.default_taker_multiplier)

    def fee(self, series_ticker: str, count: int | Decimal, price: Decimal, taker: bool = True) -> Decimal:
        return self.for_series(series_ticker).fee(count, price, taker)

    def fee_cents_per_contract(self, series_ticker: str, count: int | Decimal, price: Decimal, taker: bool = True) -> Decimal:
        return self.for_series(series_ticker).fee_cents_per_contract(count, price, taker)
