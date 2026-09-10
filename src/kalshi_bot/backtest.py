"""Decision replay backtester.

There is no synthetic history here. The bot logs a model probability for every market it prices
(accepted or not) and the quotes it saw; once those markets settle, this module scores them:

* Brier score of the model vs the market-implied probability (the YES price it saw).
* Calibration table by probability decile.
* Pessimistic P&L of the candidates that passed the model stage: filled one tick worse than the
  quoted price, taker fee at that price, no partial credit for size we could not have gotten.

It also prints what would make a good-looking result wrong, because CLAUDE.md requires it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .client import KalshiClient
from .errors import ApiError, UnexpectedApiResponse
from .fees import quadratic_fee
from .storage import Storage

log = logging.getLogger(__name__)
TICK = Decimal("0.01")


@dataclass
class BacktestReport:
    since: float
    n_scored: int = 0
    brier_model: float | None = None
    brier_market: float | None = None
    calibration: list[dict[str, Any]] = field(default_factory=list)
    n_candidates: int = 0
    pnl_cents: Decimal = Decimal("0")
    pnl_by_strategy: dict[str, Decimal] = field(default_factory=dict)
    hit_rate: float | None = None
    unresolved: int = 0
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"since": self.since, "n_scored": self.n_scored, "brier_model": self.brier_model, "brier_market": self.brier_market,
                "calibration": self.calibration, "n_candidates": self.n_candidates, "pnl_cents": str(self.pnl_cents),
                "pnl_by_strategy": {k: str(v) for k, v in self.pnl_by_strategy.items()}, "hit_rate": self.hit_rate,
                "unresolved": self.unresolved, "caveats": self.caveats}


async def sync_results(storage: Storage, client: KalshiClient | None) -> int:
    """Fetch settlement results for markets we priced but have not resolved yet."""
    if client is None:
        return 0
    tickers = storage.unresolved_decision_markets()
    n = 0
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        try:
            markets = await client.markets(tickers=chunk, status=None, limit=100, max_pages=2)
        except (ApiError, UnexpectedApiResponse) as e:
            log.warning("result sync failed: %s", e)
            continue
        for m in markets:
            if m.result in ("yes", "no"):
                storage.save_market_result(m.ticker, m.event_ticker, m.result, m.expiration_time.timestamp() if m.expiration_time else None)
                n += 1
    return n


def run_backtest(storage: Storage, since_days: float = 30.0, fee_multiplier: Decimal = Decimal("0.07")) -> BacktestReport:
    since = time.time() - since_days * 86400
    rep = BacktestReport(since=since)
    rows = [r for r in storage.decisions(since=since, limit=100000) if r["stage"] == "model" and r["model_prob"]]
    results = storage.market_results({r["market_ticker"] for r in rows})
    # keep the latest model view per market to avoid counting one market many times per scan
    latest: dict[str, dict[str, Any]] = {}
    for r in sorted(rows, key=lambda r: r["ts"]):
        latest[r["market_ticker"]] = r
    scored = [(r, results.get(r["market_ticker"])) for r in latest.values()]
    rep.unresolved = sum(1 for _, y in scored if y not in ("yes", "no"))
    scored = [(r, 1.0 if y == "yes" else 0.0) for r, y in scored if y in ("yes", "no")]
    rep.n_scored = len(scored)
    if scored:
        bm = bmk = 0.0
        buckets: dict[int, list[tuple[float, float]]] = {}
        for r, y in scored:
            p = float(r["model_prob"])
            mkt = float(r["price"]) if r["price"] else None
            bm += (p - y) ** 2
            if mkt is not None:
                bmk += (mkt - y) ** 2
            buckets.setdefault(min(9, int(p * 10)), []).append((p, y))
        rep.brier_model = bm / len(scored)
        rep.brier_market = bmk / len(scored)
        rep.calibration = [{"bucket": f"{b / 10:.1f}-{(b + 1) / 10:.1f}", "n": len(v), "mean_p": sum(p for p, _ in v) / len(v),
                            "realized": sum(y for _, y in v) / len(v)} for b, v in sorted(buckets.items())]

    # pessimistic P&L on accepted model candidates
    cands = [r for r in rows if r["accepted"] == 1 and r["book_side"] in ("bid", "ask") and r["count"]]
    hits = 0
    latest_c: dict[str, dict[str, Any]] = {}
    for r in sorted(cands, key=lambda r: r["ts"]):
        latest_c[r["market_ticker"]] = r
    for r in latest_c.values():
        y = results.get(r["market_ticker"])
        if y not in ("yes", "no"):
            continue
        rep.n_candidates += 1
        price = Decimal(r["price"])
        count = int(Decimal(r["count"]))
        yv = Decimal(1 if y == "yes" else 0)
        if r["book_side"] == "bid":
            fill = min(price + TICK, Decimal("0.99"))
            gross = (yv - fill) * count
        else:
            fill = max(price - TICK, Decimal("0.01"))
            gross = (fill - yv) * count  # short YES at fill: profit when it settles NO
        fee = quadratic_fee(count, fill, fee_multiplier)
        pnl = (gross - fee) * 100
        rep.pnl_cents += pnl
        rep.pnl_by_strategy[r["strategy"]] = rep.pnl_by_strategy.get(r["strategy"], Decimal("0")) + pnl
        if pnl > 0:
            hits += 1
    rep.hit_rate = hits / rep.n_candidates if rep.n_candidates else None
    rep.caveats = caveats(rep)
    return rep


def caveats(rep: BacktestReport) -> list[str]:
    out = [
        f"Sample: {rep.n_scored} scored markets, {rep.n_candidates} candidates, {rep.unresolved} unresolved. Buckets of one event settle together, so the effective sample is closer to the number of events than of markets.",
        "Fills are assumed one tick worse than the quote with taker fees; IOC orders may not have filled at all for the full size, and any fill implies someone was willing to take the other side.",
        "Only markets with two-sided books inside the gate were priced; selection bias toward liquid, well-priced markets is built in.",
        "Weather: the forecast used was the one available at scan time, but NWS grid forecasts update continuously; a Brier edge vanishes if the station in rules_primary differs from the configured station.",
        "Crypto: realized volatility is backward-looking; regime shifts and the CF Benchmarks vs Kraken basis are not modeled.",
        "Fees are scored with the general 0.07 multiplier; series with maker fees or special multipliers differ.",
        "A positive P&L over a few days is consistent with pure luck; require Brier(model) < Brier(market) over hundreds of independent events before believing an edge.",
    ]
    if rep.brier_model is not None and rep.brier_market is not None and rep.brier_model >= rep.brier_market:
        out.insert(0, "The model's Brier score is NOT better than the market's own prices. Any positive P&L here is noise, not edge.")
    return out
