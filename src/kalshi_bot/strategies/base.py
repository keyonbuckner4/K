from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..fees import FeeSchedule
from ..gate import GateConfig
from ..intent import DIRECTIONAL, Intent, Leg
from ..models import Event, Market, series_ticker_of
from ..orderbook import OrderBook
from ..storage import Storage

log = logging.getLogger(__name__)


@dataclass
class ScanContext:
    now: datetime
    events: list[Event]
    books: dict[str, OrderBook]
    fee_sched: FeeSchedule
    gate: GateConfig
    storage: Storage
    extras: dict[str, Any] = field(default_factory=dict)

    def book(self, ticker: str) -> OrderBook | None:
        return self.books.get(ticker)


class Strategy:
    name = "base"
    settlement_strategy = False

    def __init__(self, cfg: dict[str, Any], storage: Storage):
        self.cfg = cfg or {}
        self.storage = storage
        self.mode = str(self.cfg.get("mode", "observe"))
        self.enabled = bool(self.cfg.get("enabled", True))
        self.max_contracts = int(self.cfg.get("max_contracts", self.cfg.get("max_contracts_per_leg", 5)))
        self.discovered: set[str] = set()

    def series(self) -> list[str]:
        return [str(s) for s in self.cfg.get("series", [])] + sorted(self.discovered)

    def series_patterns(self) -> list[str]:
        """Ticker prefixes this strategy will adopt when the engine discovers them, from
        ``series_patterns`` in config. Empty means the strategy only trades what config names."""
        raw = self.cfg.get("series_patterns") or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(x).upper() for x in raw]

    def match_categories(self) -> list[str]:
        """Category substrings this strategy will adopt, from ``categories`` in config.

        Matching on category rather than ticker is what makes families like oil or economic releases
        reachable at all: their series tickers are not something to guess, but Kalshi labels the
        events, and a substring like "commodit" finds them whatever they end up being called."""
        raw = self.cfg.get("categories") or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(x).lower() for x in raw]

    def discovers(self) -> bool:
        return bool(self.series_patterns() or self.match_categories())

    def wants(self, profile: Any) -> bool:
        """Whether a discovered series belongs to this strategy: ticker prefix or event category."""
        t = str(profile.series_ticker).upper()
        if any(t.startswith(pat) for pat in self.series_patterns()):
            return True
        cats = self.match_categories()
        if not cats:
            return False
        hay = f"{profile.category or ''} {profile.sample_title or ''}".lower()
        return any(c in hay for c in cats)

    def adopt_series(self, profiles: list[Any]) -> list[str]:
        """Take on the open series matching this strategy's patterns. Returns the newly added tickers."""
        explicit = {str(s).upper() for s in self.cfg.get("series", [])}
        added = []
        for p in profiles:
            t = str(p.series_ticker).upper()
            if t in explicit or t in self.discovered or not self.wants(p):
                continue
            self.discovered.add(t)
            added.append(t)
        return added

    async def scan(self, ctx: ScanContext) -> list[Intent]:  # pragma: no cover - abstract
        raise NotImplementedError

    # ---- helpers ----------------------------------------------------------------------------
    def reject(self, stage: str, reason: str, market: Market | None = None, event_ticker: str | None = None, **kw: Any) -> None:
        self.storage.log_decision(self.name, stage, False, reason, event_ticker=event_ticker or (market.event_ticker if market else None),
                                  market_ticker=market.ticker if market else None, **kw)

    def size_for(self, available: Decimal, gate: GateConfig, cap: int | None = None) -> int:
        cap = self.max_contracts if cap is None else cap
        if gate.min_depth_multiple <= 0:
            return cap
        return int(min(cap, math.floor(available / gate.min_depth_multiple)))

    def directional_candidate(self, market: Market, book: OrderBook | None, p_yes: Decimal, ctx: ScanContext, reason: str,
                              category: str | None = None) -> Intent | None:
        """Log the model view on this market and return an Intent if the better side clears the gate's edge."""
        series = series_ticker_of(market.event_ticker, market.ticker)
        if book is None or book.best_yes_ask is None or book.best_yes_bid is None:
            self.reject("model", "no two-sided book", market, model_prob=p_yes, details={"reason": reason})
            return None
        if not ctx.fee_sched.known(series or ""):
            self.reject("model", f"fee parameters unknown for series {series}", market, model_prob=p_yes)
            return None
        ask, bid = book.best_yes_ask, book.best_yes_bid
        cands: list[tuple[Decimal, Decimal, str, Decimal, Decimal, Decimal]] = []  # (net, gross, side, price, fee, avail)
        # buy YES at the ask
        avail_b = book.yes_available_to_buy(ask)
        n_b = max(1, self.size_for(avail_b, ctx.gate))
        fee_b = ctx.fee_sched.fee_cents_per_contract(series, n_b, ask)
        gross_b = (p_yes - ask) * 100
        cands.append((gross_b - fee_b, gross_b, "bid", ask, fee_b, avail_b))
        # buy NO at (1 - bid): sell YES at the bid
        avail_a = book.yes_available_to_sell(bid)
        n_a = max(1, self.size_for(avail_a, ctx.gate))
        fee_a = ctx.fee_sched.fee_cents_per_contract(series, n_a, bid)
        gross_a = ((Decimal("1") - p_yes) - (Decimal("1") - bid)) * 100
        cands.append((gross_a - fee_a, gross_a, "ask", bid, fee_a, avail_a))
        net, gross, side, price, fee, avail = max(cands, key=lambda c: c[0])
        details = {"reason": reason, "yes_bid": str(bid), "yes_ask": str(ask), "side": side}
        if net < ctx.gate.min_net_edge_cents:
            self.reject("model", f"best side {side}: net edge {net:.2f}c < {ctx.gate.min_net_edge_cents}c", market, model_prob=p_yes,
                        book_side=side, price=price, edge_gross_cents=gross, fee_cents=fee, edge_net_cents=net, details=details)
            return None
        count = self.size_for(avail, ctx.gate)
        if count < 1:
            self.reject("model", f"edge {net:.2f}c but resting size {avail} too thin for even 1 contract", market, model_prob=p_yes,
                        book_side=side, price=price, edge_gross_cents=gross, fee_cents=fee, edge_net_cents=net, details=details)
            return None
        self.storage.log_decision(self.name, "model", True, f"candidate {side} {count} @ {price}: net edge {net:.2f}c ({reason})",
                                  event_ticker=market.event_ticker, market_ticker=market.ticker, book_side=side, price=price, count=count,
                                  model_prob=p_yes, edge_gross_cents=gross, fee_cents=fee, edge_net_cents=net, details=details)
        leg = Leg(market.ticker, side, price, count, market=market, model_prob=p_yes, fee_cents_per_contract=fee, reason=reason)
        return Intent(self.name, market.event_ticker, [leg], kind=DIRECTIONAL, expected_edge_cents=net, reason=reason,
                      settlement_strategy=self.settlement_strategy, category=category or market.category)
