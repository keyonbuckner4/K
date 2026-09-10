"""Probability math shared by the model strategies.

A market's YES condition is derived from ``strike_type`` / ``floor_strike`` / ``cap_strike``:
``greater`` (X > floor), ``greater_or_equal``, ``less`` (X < cap), ``less_or_equal``, ``between``
(floor <= X <= cap). Anything else is unpriceable and returns ``None`` rather than a guess.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from .models import Market

INF = float("inf")


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


@dataclass(frozen=True)
class Condition:
    lo: float            # -inf allowed
    hi: float            # +inf allowed
    lo_inclusive: bool
    hi_inclusive: bool

    def is_integer_bucket(self) -> bool:
        return all(v in (INF, -INF) or float(v).is_integer() for v in (self.lo, self.hi))


def market_condition(market: Market) -> Condition | None:
    st = (market.strike_type or "").lower()
    f = float(market.floor_strike) if market.floor_strike is not None else None
    c = float(market.cap_strike) if market.cap_strike is not None else None
    if st == "greater" and f is not None:
        return Condition(f, INF, False, False)
    if st == "greater_or_equal" and f is not None:
        return Condition(f, INF, True, False)
    if st == "less" and c is not None:
        return Condition(-INF, c, False, False)
    if st == "less_or_equal" and c is not None:
        return Condition(-INF, c, True, True)
    if st == "between" and f is not None and c is not None:
        return Condition(f, c, True, True)
    return None


def real_interval(cond: Condition, integer_settlement: bool) -> tuple[float, float]:
    """Open interval (a, b) on the real line such that P(YES) = P(a < X < b) for a continuous X.
    With integer settlement, inclusive integer bounds are widened by 0.5 (continuity correction)
    and exclusive integer bounds are moved by 0.5 the other way."""
    a, b = cond.lo, cond.hi
    if integer_settlement:
        if a != -INF and float(a).is_integer():
            a = a - 0.5 if cond.lo_inclusive else a + 0.5
        if b != INF and float(b).is_integer():
            b = b + 0.5 if cond.hi_inclusive else b - 0.5
    return a, b


def prob_normal_interval(a: float, b: float, mean: float, sd: float) -> float:
    if sd <= 0:
        return 1.0 if a < mean < b else 0.0
    lo = 0.0 if a == -INF else norm_cdf((a - mean) / sd)
    hi = 1.0 if b == INF else norm_cdf((b - mean) / sd)
    return max(0.0, min(1.0, hi - lo))


def prob_yes_normal(market: Market, mean: float, sd: float, integer_settlement: bool) -> float | None:
    cond = market_condition(market)
    if cond is None:
        return None
    a, b = real_interval(cond, integer_settlement)
    return prob_normal_interval(a, b, mean, sd)


def prob_yes_lognormal(market: Market, spot: float, sigma_annual: float, tau_years: float) -> float | None:
    """Barrier-style probability for a price level at expiry under driftless lognormal dynamics."""
    cond = market_condition(market)
    if cond is None or spot <= 0 or tau_years <= 0 or sigma_annual <= 0:
        return None
    sd = sigma_annual * math.sqrt(tau_years)
    mean = math.log(spot) - 0.5 * sd * sd
    a = -INF if cond.lo <= 0 or cond.lo == -INF else math.log(cond.lo)
    b = INF if cond.hi == INF else math.log(cond.hi)
    return prob_normal_interval(a, b, mean, sd)


def to_decimal_prob(p: float | None) -> Decimal | None:
    return None if p is None else Decimal(str(round(p, 6)))
