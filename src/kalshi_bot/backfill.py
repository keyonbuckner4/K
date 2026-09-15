"""Score a model against markets that have already settled.

Forward observation is limited by how fast markets settle: the daily crypto ladders settle once a
day, so a verdict that needs 20 contested settlements spread over several days takes a week of
waiting. This module gets the same evidence from history instead. For every already-settled market
it replays the model at each point in the market's life, using only data that existed at that
moment (Kraken spot, Deribit DVOL, and Kalshi's own candlesticks for what the market was charging),
applies the same gate, and writes one decision row per market into a scratch database that
``run_backtest`` then scores with exactly the live verdict logic.

What this can and cannot tell you, stated up front because the numbers look like the live ones:

* It CAN tell you whether the model's probabilities beat the prices the market was actually
  charging, over hundreds of settled markets. That is the question the live scorecard needs a week
  to answer, and it is the question that decides whether an edge exists at all.
* It CANNOT tell you whether the orders would have filled. Candlesticks carry no order book, so
  there is no depth and no queue position. A backfill that says "edge" is necessary evidence, not
  sufficient: the live observe run is still what proves the edge survives execution.
* It CANNOT cover the weather model. Scoring that one needs the NWS forecast as it stood at decision
  time, and the public API serves only the current forecast. Weather has to be observed forward.

Nothing here interpolates. A gap in the spot or volatility history becomes a skipped decision point
with a counted reason, never an invented price.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .backtest import BacktestReport, run_backtest
from .client import KalshiClient
from .data.history import HistoryFeed
from .errors import DataUnavailable, UnexpectedApiResponse
from .fees import FeeSchedule
from .gate import GateConfig
from .models import Market, dec
from .pricing import prob_yes_lognormal, to_decimal_prob
from .storage import Storage

log = logging.getLogger(__name__)
SECONDS_PER_YEAR = 365.0 * 86400.0
_TS_KEYS = ("end_period_ts", "end_ts", "period_end_ts", "ts", "timestamp")
_SUB_KEYS = ("yes_bid", "yes_ask", "price")


@dataclass
class Candle:
    ts: float
    yes_bid: Decimal | None = None
    yes_ask: Decimal | None = None
    price: Decimal | None = None

    def market_prob(self) -> Decimal | None:
        """What the market was charging, as a probability: the midpoint when two-sided, else the traded price."""
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2
        return self.price


def detect_price_scale(items: Sequence[Mapping[str, Any]]) -> str:
    """'dollars' or 'cents', decided once per market from every raw value rather than per value.

    Kalshi has served both: ``*_dollars`` keys carry dollar strings, the legacy integer fields carry
    cents. Guessing per value misreads a whole-dollar price, so the convention is settled by looking
    for any explicit ``_dollars`` key or any non-integer value across the whole response.
    """
    saw_value = False
    for item in items:
        for sub_key in _SUB_KEYS:
            sub = item.get(sub_key)
            if not isinstance(sub, Mapping):
                continue
            for k, v in sub.items():
                if k.endswith("_dollars") and v not in (None, ""):
                    return "dollars"
                if v in (None, ""):
                    continue
                if isinstance(v, str) and "." in v:
                    return "dollars"
                if isinstance(v, float) and not float(v).is_integer():
                    return "dollars"
                saw_value = True
    if not saw_value:
        raise UnexpectedApiResponse("candlesticks carry no price values", {"first_item": dict(items[0]) if items else None})
    return "cents"


def _value(sub: Mapping[str, Any], key: str, scale: str) -> Decimal | None:
    raw = sub.get(f"{key}_dollars")
    if raw not in (None, ""):
        return dec(raw)
    raw = sub.get(key)
    if raw in (None, ""):
        return None
    d = dec(raw)
    if d is None:
        return None
    return d / 100 if scale == "cents" else d


def parse_candles(items: Sequence[Mapping[str, Any]]) -> list[Candle]:
    """Kalshi candlesticks -> timestamped yes bid/ask/price. Raises rather than guessing a shape."""
    if not items:
        return []
    scale = detect_price_scale(items)
    out: list[Candle] = []
    for item in items:
        ts = next((item[k] for k in _TS_KEYS if item.get(k) not in (None, "")), None)
        if ts is None:
            raise UnexpectedApiResponse(f"candlestick without a timestamp (looked for {_TS_KEYS})", dict(item))
        c = Candle(float(ts))
        for name, attr in (("yes_bid", "yes_bid"), ("yes_ask", "yes_ask"), ("price", "price")):
            sub = item.get(name)
            if isinstance(sub, Mapping):
                v = _value(sub, "close", scale)
                if v is None:
                    v = _value(sub, "mean", scale)
                setattr(c, attr, v)
        if c.market_prob() is not None:
            out.append(c)
    out.sort(key=lambda c: c.ts)
    for c in out:
        p = c.market_prob()
        if p is not None and not (Decimal("0") <= p <= Decimal("1")):
            raise UnexpectedApiResponse(f"candlestick price {p} outside 0-1 after {scale} conversion", {"ts": c.ts})
    return out


@dataclass
class BackfillStats:
    markets_seen: int = 0
    markets_scored: int = 0
    decision_points: int = 0
    candidates: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    price_scale: str | None = None
    vol_source: str = "?"

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


async def settled_markets(client: KalshiClient, series_ticker: str, since_ts: float, max_pages: int = 20) -> list[Market]:
    """Markets in a series that have a yes/no result and settled after ``since_ts``.

    ``status`` is not filtered on: Kalshi's status vocabulary for finished markets is not something
    to guess at, so the result field decides and the statuses actually seen are logged.
    """
    markets = await client.markets(series_ticker=series_ticker, status=None, limit=1000,
                                   min_close_ts=int(since_ts), max_pages=max_pages)
    statuses = {m.status for m in markets}
    out = [m for m in markets if (m.result or "").lower() in ("yes", "no")]
    log.info("%s: %d markets fetched, %d settled with a result (statuses seen: %s)", series_ticker, len(markets), len(out), sorted(statuses))
    return out


async def backfill_crypto(client: KalshiClient, history: HistoryFeed, scratch: Storage, *, series_map: Mapping[str, str],
                          days: float, gate: GateConfig, fee_sched: FeeSchedule, max_contracts: int = 5,
                          min_minutes_to_close: float = 20.0, realized_window_hours: float = 72.0,
                          vol_source: str = "deribit_dvol", now: float | None = None) -> tuple[BacktestReport, BackfillStats]:
    now = time.time() if now is None else now
    since = now - days * 86400
    stats = BackfillStats()

    for asset, series_ticker in series_map.items():
        markets = await settled_markets(client, series_ticker, since)
        stats.markets_seen += len(markets)
        if not markets:
            continue
        spot = await history.spot_history(asset, since, now)
        dvol = None
        if vol_source == "deribit_dvol":
            try:
                dvol = await history.dvol_history(asset, since, now)
                stats.vol_source = "deribit_dvol"
            except DataUnavailable as e:
                log.warning("DVOL history unavailable for %s (%s); scoring with trailing realized vol instead", asset, e)
                stats.vol_source = f"realized_{realized_window_hours:.0f}h (DVOL history unavailable)"
        if dvol is None and stats.vol_source == "?":
            stats.vol_source = f"realized_{realized_window_hours:.0f}h"

        for m in markets:
            settle = m.expiration_time or m.close_time
            if settle is None:
                stats.skip("no settlement time")
                continue
            settle_ts = settle.timestamp()
            try:
                raw = await client.candlesticks(series_ticker, m.ticker, int(settle_ts - 2 * 86400), int(settle_ts), period_interval=60)
            except (UnexpectedApiResponse, DataUnavailable) as e:
                log.warning("candlesticks unavailable for %s: %s", m.ticker, e)
                stats.skip("candlesticks unavailable")
                continue
            candles = parse_candles(raw)
            if stats.price_scale is None and raw:
                stats.price_scale = detect_price_scale(raw)
            if not candles:
                stats.skip("no candles")
                continue

            chosen: dict[str, Any] | None = None
            last_priced: dict[str, Any] | None = None
            for c in candles:
                tau_s = settle_ts - c.ts
                if tau_s < min_minutes_to_close * 60:
                    continue
                try:
                    s = spot.at(c.ts)
                    sigma = dvol.at(c.ts) if dvol is not None else spot.realized_vol_at(c.ts, realized_window_hours)
                except DataUnavailable:
                    stats.skip("no spot/vol at that moment")
                    continue
                p = prob_yes_lognormal(m, s, sigma, tau_s / SECONDS_PER_YEAR)
                if p is None:
                    stats.skip(f"unpriceable strike_type {m.strike_type!r}")
                    break
                stats.decision_points += 1
                mkt = c.market_prob()
                row = {"ts": c.ts, "p": Decimal(str(round(p, 6))), "price": mkt, "side": None, "count": None,
                       "reason": f"replay {asset} spot {s:.2f} sigma {sigma:.2%} tau {tau_s / 3600:.2f}h"}
                last_priced = row
                if chosen is not None or c.yes_bid is None or c.yes_ask is None:
                    if c.yes_bid is None or c.yes_ask is None:
                        stats.skip("candle not two-sided: priced but not tradeable")
                    continue
                if (c.yes_ask - c.yes_bid) * 100 > gate.max_spread_cents:
                    continue
                for side, price in (("bid", c.yes_ask), ("ask", c.yes_bid)):
                    if not (gate.min_market_price < price < gate.max_market_price):
                        continue
                    p_out = row["p"] if side == "bid" else (Decimal("1") - row["p"])
                    outcome_price = price if side == "bid" else (Decimal("1") - price)
                    fee = fee_sched.fee_cents_per_contract(series_ticker, max_contracts, price, taker=True)
                    net = (p_out - outcome_price) * 100 - fee
                    if net >= gate.min_net_edge_cents:
                        chosen = dict(row, side=side, price=price, count=max_contracts,
                                      reason=f"candidate {side} {max_contracts} @ {price}: net edge {net:.2f}c ({row['reason']})")
                        stats.candidates += 1
                        break

            use = chosen or last_priced
            if use is None:
                stats.skip("no usable decision point")
                continue
            scratch.log_decision("crypto", "model", chosen is not None, str(use["reason"]), event_ticker=m.event_ticker,
                                 market_ticker=m.ticker, book_side=use["side"], price=use["price"], count=use["count"],
                                 model_prob=use["p"], ts=use["ts"])
            scratch.save_market_result(m.ticker, m.event_ticker, (m.result or "").lower(), settle_ts)
            stats.markets_scored += 1

    rep = run_backtest(scratch, since_days=days + 1, since_model_change=False, now=now)
    rep.caveats.insert(0, "BACKFILL, not a live run: scored from Kalshi candlesticks, which carry no order book. "
                          "Depth and queue position are unknown, so fills are assumed and the P&L is optimistic even "
                          "after the one-tick penalty. An edge here still has to survive a live observe run.")
    rep.caveats.insert(1, f"Volatility input: {stats.vol_source}. Prices read as {stats.price_scale or 'n/a'}. "
                          f"{stats.decision_points} decision points over {stats.markets_scored} settled markets.")
    return rep, stats
