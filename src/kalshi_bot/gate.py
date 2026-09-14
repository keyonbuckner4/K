"""Entry gate. Every strategy passes all four checks (BRIEF.md):
1. net edge after fees >= 5 cents;  2. spread <= 3 cents;  3. resting size at my price >= 2x my
size;  4. market settles in >= 10 minutes. Rejections are returned with reasons so they get logged."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from .fees import FeeSchedule
from .intent import ARB, Intent, Leg
from .models import Market, series_ticker_of
from .orderbook import OrderBook


@dataclass(frozen=True)
class GateConfig:
    min_net_edge_cents: Decimal = Decimal("5")
    max_spread_cents: Decimal = Decimal("3")
    min_depth_multiple: Decimal = Decimal("2")
    min_minutes_to_settlement: int = 10
    # 5. a directional leg must not bet against a market that is already near certain: the first observe
    # period showed every such "edge" was model error, and a market at 3c or 97c is right ~97% of the time.
    min_market_price: Decimal = Decimal("0.05")
    max_market_price: Decimal = Decimal("0.95")

    @classmethod
    def from_toml(cls, cfg: Mapping[str, Any] | None) -> "GateConfig":
        c = cfg or {}
        return cls(Decimal(str(c.get("min_net_edge_cents", 5))), Decimal(str(c.get("max_spread_cents", 3))),
                   Decimal(str(c.get("min_depth_multiple", 2))), int(c.get("min_minutes_to_settlement", 10)),
                   Decimal(str(c.get("min_market_price", "0.05"))), Decimal(str(c.get("max_market_price", "0.95"))))


@dataclass
class GateResult:
    ok: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


def leg_edge_cents(leg: Leg, fee_sched: FeeSchedule, series_ticker: str | None) -> tuple[Decimal | None, Decimal, Decimal | None]:
    """(gross edge, fee per contract, net edge) in cents for a model-priced leg, or (None, fee, None)."""
    fee = fee_sched.fee_cents_per_contract(series_ticker, leg.count, leg.price, taker=True) if series_ticker else Decimal("0")
    if leg.model_prob is None:
        return None, fee, None
    p_outcome = leg.model_prob if leg.book_side == "bid" else (Decimal("1") - leg.model_prob)
    gross = (p_outcome - leg.outcome_price) * 100
    return gross, fee, gross - fee


def check_leg(leg: Leg, book: OrderBook | None, market: Market | None, fee_sched: FeeSchedule, now: datetime,
              cfg: GateConfig, skip_edge: bool = False) -> GateResult:
    d: dict[str, Any] = {"ticker": leg.ticker, "book_side": leg.book_side, "price": str(leg.price), "count": leg.count}
    market = market or leg.market
    if book is None:
        return GateResult(False, "no order book", d)
    if market is None:
        return GateResult(False, "no market metadata", d)
    series = series_ticker_of(market.event_ticker, market.ticker)

    # 4. settlement window (checked first: cheapest and most common rejection)
    st = market.settle_time
    if st is None:
        return GateResult(False, "settlement time unknown", d)
    minutes = (st - now).total_seconds() / 60
    d["minutes_to_settlement"] = round(minutes, 1)
    if minutes < cfg.min_minutes_to_settlement:
        return GateResult(False, f"settles in {minutes:.1f} min < {cfg.min_minutes_to_settlement}", d)

    # 2. spread
    spread = book.spread_cents
    d["spread_cents"] = str(spread) if spread is not None else None
    if spread is None:
        return GateResult(False, "one-sided book (no bid or no ask)", d)
    if spread > cfg.max_spread_cents:
        return GateResult(False, f"spread {spread}c > {cfg.max_spread_cents}c", d)

    # 3. depth at my price
    avail = book.yes_available_to_buy(leg.price) if leg.book_side == "bid" else book.yes_available_to_sell(leg.price)
    need = Decimal(leg.count) * cfg.min_depth_multiple
    d["available"] = str(avail)
    if avail < need:
        return GateResult(False, f"resting size {avail} at {leg.price} < {cfg.min_depth_multiple}x my {leg.count}", d)

    # 5. near-certain market (directional legs only; arb legs are structural)
    if not skip_edge and not (cfg.min_market_price < leg.price < cfg.max_market_price):
        return GateResult(False, f"market at {leg.price} is near certain (outside {cfg.min_market_price}-{cfg.max_market_price}); not betting against it", d)

    # 1. net edge
    gross, fee, net = leg_edge_cents(leg, fee_sched, series)
    d.update({"fee_cents_per_contract": str(fee), "edge_gross_cents": str(gross) if gross is not None else None,
              "edge_net_cents": str(net) if net is not None else None})
    leg.fee_cents_per_contract = fee
    if skip_edge:
        return GateResult(True, "ok (edge checked at basket level)", d)
    if net is None:
        return GateResult(False, "no model probability on a directional leg", d)
    if net < cfg.min_net_edge_cents:
        return GateResult(False, f"net edge {net:.2f}c < {cfg.min_net_edge_cents}c", d)
    return GateResult(True, "ok", d)


def check_intent(intent: Intent, books: Mapping[str, OrderBook], fee_sched: FeeSchedule, now: datetime, cfg: GateConfig) -> list[GateResult]:
    """Run the gate on every leg. Arb baskets have their edge checked on the basket."""
    results = [check_leg(leg, books.get(leg.ticker), leg.market, fee_sched, now, cfg, skip_edge=(intent.kind == ARB)) for leg in intent.legs]
    if intent.kind == ARB and all(r.ok for r in results):
        if intent.expected_edge_cents < cfg.min_net_edge_cents:
            results.append(GateResult(False, f"basket net edge {intent.expected_edge_cents:.2f}c < {cfg.min_net_edge_cents}c",
                                      {"event_ticker": intent.event_ticker}))
    return results
