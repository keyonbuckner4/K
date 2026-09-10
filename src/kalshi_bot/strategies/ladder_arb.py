"""Ladder arbitrage: mechanical edges inside one event, no forecast required.

1. Exhaustive, mutually exclusive ladders (temperature buckets, price ranges): exactly one
   market settles YES, so the YES prices should sum to ~$1.
   * sum of best YES asks < 1  -> buy YES on every leg; payout is exactly $1 per basket.
   * sum of best YES bids > 1  -> sell YES (buy NO) on every leg; payout is (n-1) dollars.
2. Nested thresholds (``greater``/``less`` families): a looser condition cannot be cheaper than a
   stricter one. If ask(looser) < bid(stricter) the pair pays at least $1 for less than $1.
3. One contract whose YES ask + NO ask < 1 is a crossed book; logged and traded as a 2-leg basket.

Every detected gap is logged to ``arb_gaps`` with its net edge after fees, even when it is too
small to trade, so the observe-only phase produces the evidence the BRIEF asks for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..intent import ARB, Intent, Leg
from ..models import Event, Market, series_ticker_of
from ..orderbook import OrderBook
from ..pricing import INF, Condition, market_condition
from .base import ScanContext, Strategy

log = logging.getLogger(__name__)
ONE = Decimal("1")


@dataclass
class Rung:
    market: Market
    cond: Condition
    book: OrderBook


def is_exhaustive_ladder(rungs: list[Rung]) -> bool:
    """True when the conditions tile the real line without overlap (so exactly one settles YES)."""
    if len(rungs) < 2:
        return False
    rs = sorted(rungs, key=lambda r: (r.cond.lo, r.cond.hi))
    if rs[0].cond.lo != -INF or rs[-1].cond.hi != INF:
        return False
    for prev, cur in zip(rs, rs[1:]):
        a, b = prev.cond, cur.cond
        if a.hi == INF or b.lo == -INF:
            return False
        touching = a.hi == b.lo and (a.hi_inclusive != b.lo_inclusive)
        integer_step = (a.hi_inclusive and b.lo_inclusive and float(a.hi).is_integer() and float(b.lo).is_integer() and b.lo == a.hi + 1)
        cent_step = (a.hi_inclusive and b.lo_inclusive and abs((b.lo - a.hi) - 0.01) < 1e-9)
        if not (touching or integer_step or cent_step):
            return False
    return True


class LadderArbStrategy(Strategy):
    name = "ladder_arb"

    def __init__(self, cfg: dict[str, Any], storage):
        super().__init__(cfg, storage)
        self.max_legs = int(self.cfg.get("max_legs", 12))

    async def scan(self, ctx: ScanContext) -> list[Intent]:
        intents: list[Intent] = []
        for event in ctx.events:
            try:
                intents.extend(self._scan_event(event, ctx))
            except Exception:  # one bad event must not stop the scan; it is logged with its reason
                log.exception("ladder_arb: failed scanning %s", event.event_ticker)
                self.reject("scan", "exception during scan (see log)", event_ticker=event.event_ticker)
        return intents

    def _rungs(self, event: Event, ctx: ScanContext) -> list[Rung]:
        rungs: list[Rung] = []
        for m in event.markets:
            if not m.is_open():
                continue
            cond = market_condition(m)
            book = ctx.book(m.ticker)
            if cond is None:
                self.reject("ladder", f"unpriceable strike_type {m.strike_type!r}", m)
                continue
            if book is None or book.stale:
                self.reject("ladder", "no fresh order book", m)
                continue
            rungs.append(Rung(m, cond, book))
        return rungs

    def _scan_event(self, event: Event, ctx: ScanContext) -> list[Intent]:
        rungs = self._rungs(event, ctx)
        out: list[Intent] = []
        series = event.series_ticker or series_ticker_of(event.event_ticker) or ""
        if not ctx.fee_sched.known(series):
            self.reject("ladder", f"fee parameters unknown for series {series}", event_ticker=event.event_ticker)
            return out
        for r in rungs:  # 3. crossed books on a single contract
            if r.book.is_crossed():
                out.extend(self._crossed(event, r, series, ctx))
        if len(rungs) >= 2 and event.mutually_exclusive and is_exhaustive_ladder(rungs) and len(rungs) <= self.max_legs:
            out.extend(self._sum_arbs(event, rungs, series, ctx))
        elif len(rungs) >= 2 and event.mutually_exclusive:
            self.reject("ladder", f"mutually exclusive but not an exhaustive ladder ({len(rungs)} rungs); sum arb skipped",
                        event_ticker=event.event_ticker)
        out.extend(self._monotonicity(event, rungs, series, ctx))
        return out

    # ---- 1. exhaustive ladders ------------------------------------------------------------------
    def _sum_arbs(self, event: Event, rungs: list[Rung], series: str, ctx: ScanContext) -> list[Intent]:
        out: list[Intent] = []
        asks = [(r, r.book.best_yes_ask, r.book.yes_ask_size) for r in rungs]
        bids = [(r, r.book.best_yes_bid, r.book.yes_bid_size) for r in rungs]
        if all(a is not None for _, a, _ in asks):
            total = sum(a for _, a, _ in asks)
            gross = (ONE - total) * 100
            size = self.size_for(min(q for _, _, q in asks), ctx.gate)
            fee = sum(ctx.fee_sched.fee_cents_per_contract(series, max(size, 1), a) for _, a, _ in asks)
            net = gross - fee
            legs = [Leg(r.market.ticker, "bid", a, max(size, 1), market=r.market, fee_cents_per_contract=ctx.fee_sched.fee_cents_per_contract(series, max(size, 1), a),
                        reason="buy-all-YES ladder") for r, a, _ in asks]
            if gross > 0:
                self.storage.log_arb_gap(event.event_ticker, "sum_yes_asks_below_1", len(asks), total, gross, fee, net, size, [l.to_dict() for l in legs])
            out.extend(self._emit(event, "sum_yes_asks_below_1", legs, gross, fee, net, size, ctx, f"YES asks sum to {total}"))
        if all(b is not None for _, b, _ in bids):
            total = sum(b for _, b, _ in bids)
            gross = (total - ONE) * 100
            size = self.size_for(min(q for _, _, q in bids), ctx.gate)
            fee = sum(ctx.fee_sched.fee_cents_per_contract(series, max(size, 1), b) for _, b, _ in bids)
            net = gross - fee
            legs = [Leg(r.market.ticker, "ask", b, max(size, 1), market=r.market, fee_cents_per_contract=ctx.fee_sched.fee_cents_per_contract(series, max(size, 1), b),
                        reason="sell-all-YES ladder") for r, b, _ in bids]
            if gross > 0:
                self.storage.log_arb_gap(event.event_ticker, "sum_yes_bids_above_1", len(bids), total, gross, fee, net, size, [l.to_dict() for l in legs])
            out.extend(self._emit(event, "sum_yes_bids_above_1", legs, gross, fee, net, size, ctx, f"YES bids sum to {total}"))
        return out

    # ---- 2. nested thresholds ---------------------------------------------------------------------
    def _monotonicity(self, event: Event, rungs: list[Rung], series: str, ctx: ScanContext) -> list[Intent]:
        out: list[Intent] = []
        greater = sorted([r for r in rungs if r.cond.hi == INF and r.cond.lo != -INF], key=lambda r: r.cond.lo)
        less = sorted([r for r in rungs if r.cond.lo == -INF and r.cond.hi != INF], key=lambda r: r.cond.hi, reverse=True)
        for family in (greater, less):  # ordered loosest -> strictest
            for i in range(len(family)):
                for j in range(i + 1, len(family)):
                    loose, strict = family[i], family[j]
                    ask_l, bid_s = loose.book.best_yes_ask, strict.book.best_yes_bid
                    if ask_l is None or bid_s is None or ask_l >= bid_s:
                        continue
                    gross = (bid_s - ask_l) * 100
                    size = self.size_for(min(loose.book.yes_ask_size, strict.book.yes_bid_size), ctx.gate)
                    n = max(size, 1)
                    fee = ctx.fee_sched.fee_cents_per_contract(series, n, ask_l) + ctx.fee_sched.fee_cents_per_contract(series, n, bid_s)
                    net = gross - fee
                    legs = [Leg(loose.market.ticker, "bid", ask_l, n, market=loose.market, reason="buy looser threshold"),
                            Leg(strict.market.ticker, "ask", bid_s, n, market=strict.market, reason="sell stricter threshold")]
                    self.storage.log_arb_gap(event.event_ticker, "threshold_monotonicity", 2, ask_l - bid_s, gross, fee, net, size, [l.to_dict() for l in legs])
                    out.extend(self._emit(event, "threshold_monotonicity", legs, gross, fee, net, size, ctx,
                                          f"ask {ask_l} on {loose.market.ticker} < bid {bid_s} on {strict.market.ticker}"))
        return out

    # ---- 3. crossed single contract --------------------------------------------------------------
    def _crossed(self, event: Event, r: Rung, series: str, ctx: ScanContext) -> list[Intent]:
        ask, bid = r.book.best_yes_ask, r.book.best_yes_bid
        gross = (bid - ask) * 100  # buy YES at ask, sell YES at bid: YES ask + NO ask < 1
        size = self.size_for(min(r.book.yes_ask_size, r.book.yes_bid_size), ctx.gate)
        n = max(size, 1)
        fee = ctx.fee_sched.fee_cents_per_contract(series, n, ask) + ctx.fee_sched.fee_cents_per_contract(series, n, bid)
        net = gross - fee
        legs = [Leg(r.market.ticker, "bid", ask, n, market=r.market, reason="crossed book buy"), Leg(r.market.ticker, "ask", bid, n, market=r.market, reason="crossed book sell")]
        self.storage.log_arb_gap(event.event_ticker, "crossed_book", 2, ask + (ONE - bid), gross, fee, net, size, [l.to_dict() for l in legs])
        return self._emit(event, "crossed_book", legs, gross, fee, net, size, ctx, f"crossed: yes ask {ask} < yes bid {bid}")

    def _emit(self, event: Event, kind: str, legs: list[Leg], gross: Decimal, fee: Decimal, net: Decimal, size: int, ctx: ScanContext,
              why: str) -> list[Intent]:
        details = {"kind": kind, "gross_cents": str(gross), "fee_cents": str(fee), "net_cents": str(net), "size": size}
        if gross <= 0:
            return []
        if net < ctx.gate.min_net_edge_cents:
            self.reject("ladder", f"{kind}: net {net:.2f}c < {ctx.gate.min_net_edge_cents}c after {fee:.2f}c fees ({why})",
                        event_ticker=event.event_ticker, edge_gross_cents=gross, fee_cents=fee, edge_net_cents=net, details=details)
            return []
        if size < 1:
            self.reject("ladder", f"{kind}: net {net:.2f}c but depth too thin for 1 contract ({why})", event_ticker=event.event_ticker,
                        edge_gross_cents=gross, fee_cents=fee, edge_net_cents=net, details=details)
            return []
        for l in legs:
            l.count = size
            l.fee_cents_per_contract = ctx.fee_sched.fee_cents_per_contract(series_ticker_of(event.event_ticker) or "", size, l.price)
        self.storage.log_decision(self.name, "ladder", True, f"{kind}: net {net:.2f}c on {size} baskets ({why})", event_ticker=event.event_ticker,
                                  edge_gross_cents=gross, fee_cents=fee, edge_net_cents=net, count=size, details=details)
        return [Intent(self.name, event.event_ticker, legs, kind=ARB, expected_edge_cents=net, reason=f"{kind}: {why}", category=event.category)]
