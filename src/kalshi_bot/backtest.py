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
from .models import dec
from .storage import Storage

log = logging.getLogger(__name__)
TICK = Decimal("0.01")


MIN_CONTESTED = 20      # contested settlements needed before a verdict is offered
CONTESTED_LO, CONTESTED_HI = 0.05, 0.95


@dataclass
class BacktestReport:
    since: float
    n_scored: int = 0
    brier_model: float | None = None            # model, over every scored market
    n_paired: int = 0                            # markets with both a model probability and a market price
    brier_model_paired: float | None = None
    brier_market: float | None = None           # market price as a probability, over the paired markets only
    n_contested: int = 0                         # paired markets with a market price in [0.05, 0.95]
    brier_model_contested: float | None = None
    brier_market_contested: float | None = None
    verdict: str = "insufficient evidence"
    calibration: list[dict[str, Any]] = field(default_factory=list)
    n_candidates: int = 0
    pnl_cents: Decimal = Decimal("0")
    pnl_by_strategy: dict[str, Decimal] = field(default_factory=dict)
    hit_rate: float | None = None
    brier_model_at_trade: float | None = None    # over the candidates: model vs market price at the moment it would have traded
    brier_market_at_trade: float | None = None
    unresolved: int = 0
    caveats: list[str] = field(default_factory=list)
    by_strategy: dict[str, dict[str, Any]] = field(default_factory=dict)   # the same scores, per strategy

    def to_dict(self) -> dict[str, Any]:
        return {"since": self.since, "n_scored": self.n_scored, "brier_model": self.brier_model, "n_paired": self.n_paired,
                "brier_model_paired": self.brier_model_paired, "brier_market": self.brier_market, "n_contested": self.n_contested,
                "brier_model_contested": self.brier_model_contested, "brier_market_contested": self.brier_market_contested,
                "verdict": self.verdict,
                "calibration": self.calibration, "n_candidates": self.n_candidates, "pnl_cents": str(self.pnl_cents),
                "pnl_by_strategy": {k: str(v) for k, v in self.pnl_by_strategy.items()}, "hit_rate": self.hit_rate,
                "brier_model_at_trade": self.brier_model_at_trade, "brier_market_at_trade": self.brier_market_at_trade,
                "unresolved": self.unresolved, "caveats": self.caveats, "by_strategy": self.by_strategy,
                "model_version": MODEL_VERSION}


RESULT_SYNC_DAYS = 45.0   # markets priced longer ago than this are no longer asked about


async def sync_results(storage: Storage, client: KalshiClient | None) -> int:
    """Fetch settlement results for markets we priced but have not resolved yet."""
    if client is None:
        return 0
    tickers = storage.unresolved_decision_markets(since=time.time() - RESULT_SYNC_DAYS * 86400)
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


MODEL_VERSION = "2026-09-14 observation-bounded weather, DVOL crypto, 5c-95c gate"


def model_version_since(storage: Storage, now: float | None = None) -> float:
    """Timestamp from which the current model version's decisions run. Recorded the first time a bot with
    this MODEL_VERSION touches the database, so a rewrite is judged on its own decisions, not its
    predecessor's."""
    state = storage.all_state()
    if state.get("model_version") != MODEL_VERSION:
        now = time.time() if now is None else now
        storage.set_state("model_version", MODEL_VERSION)
        storage.set_state("model_version_since", now)
        return now
    return float(state.get("model_version_since") or 0.0)


def run_backtest(storage: Storage, since_days: float = 30.0, fee_multiplier: Decimal = Decimal("0.07"),
                 since_model_change: bool = True, now: float | None = None) -> BacktestReport:
    """``now`` anchors the scoring window; a backfill replaying old markets must pass its own, or the
    window would be measured from wall-clock time and cut off the very decisions it just replayed."""
    now = time.time() if now is None else now
    since = now - since_days * 86400
    if since_model_change:
        since = max(since, model_version_since(storage, now))
    rep = BacktestReport(since=since)
    # the latest model view per market, resolved in SQL: a row cap here once hid every settled market
    latest = {r["market_ticker"]: r for r in storage.latest_model_decisions(since) if r["model_prob"]}
    results = storage.market_results(set(latest))
    scored = [(r, results.get(r["market_ticker"])) for r in latest.values()]
    rep.unresolved = sum(1 for _, y in scored if y not in ("yes", "no"))
    scored = [(r, 1.0 if y == "yes" else 0.0) for r, y in scored if y in ("yes", "no")]
    rep.n_scored = len(scored)
    if scored:
        bm = 0.0
        paired: list[tuple[float, float, float]] = []   # (model p, market p, outcome)
        buckets: dict[int, list[tuple[float, float]]] = {}
        per: dict[str, dict[str, Any]] = {}
        for r, y in scored:
            p = float(r["model_prob"])
            bm += (p - y) ** 2
            buckets.setdefault(min(9, int(p * 10)), []).append((p, y))
            st = per.setdefault(r["strategy"], {"n_scored": 0, "n_contested": 0, "_bm": 0.0, "_bk": 0.0})
            st["n_scored"] += 1
            if r["price"]:
                m = float(r["price"])
                paired.append((p, m, y))
                if CONTESTED_LO <= m <= CONTESTED_HI:
                    st["n_contested"] += 1
                    st["_bm"] += (p - y) ** 2
                    st["_bk"] += (m - y) ** 2
        for st in per.values():
            n = st.pop("n_contested")
            st["n_contested"] = n
            st["brier_model_contested"] = round(st.pop("_bm") / n, 4) if n else None
            st["brier_market_contested"] = round(st.pop("_bk") / n, 4) if n else None
        rep.by_strategy = per
        rep.brier_model = bm / len(scored)
        rep.calibration = [{"bucket": f"{b / 10:.1f}-{(b + 1) / 10:.1f}", "n": len(v), "mean_p": sum(p for p, _ in v) / len(v),
                            "realized": sum(y for _, y in v) / len(v)} for b, v in sorted(buckets.items())]
        if paired:
            rep.n_paired = len(paired)
            rep.brier_model_paired = sum((p - y) ** 2 for p, _, y in paired) / len(paired)
            rep.brier_market = sum((m - y) ** 2 for _, m, y in paired) / len(paired)
        contested = [t for t in paired if CONTESTED_LO <= t[1] <= CONTESTED_HI]
        if contested:
            rep.n_contested = len(contested)
            rep.brier_model_contested = sum((p - y) ** 2 for p, _, y in contested) / len(contested)
            rep.brier_market_contested = sum((m - y) ** 2 for _, m, y in contested) / len(contested)
        if rep.n_contested >= MIN_CONTESTED:
            better = rep.brier_model_contested < rep.brier_market_contested
            rep.verdict = (f"model {'beats' if better else 'does NOT beat'} the market's prices on {rep.n_contested} contested settlements "
                           f"(Brier {rep.brier_model_contested:.4f} vs {rep.brier_market_contested:.4f}, lower is better)")
        else:
            rep.verdict = (f"insufficient evidence: {rep.n_contested} contested settlements scored, need {MIN_CONTESTED} "
                           f"(markets priced between {int(CONTESTED_LO * 100)}c and {int(CONTESTED_HI * 100)}c; far-from-the-money markets prove nothing)")

    # pessimistic P&L on accepted model candidates
    latest_c = {r["market_ticker"]: r for r in storage.latest_candidate_decisions(since) if r["book_side"] in ("bid", "ask") and r["count"]}
    results.update(storage.market_results(set(latest_c) - set(results)))
    hits = 0
    bm_t = bk_t = 0.0
    for r in latest_c.values():
        y = results.get(r["market_ticker"])
        if y not in ("yes", "no"):
            continue
        rep.n_candidates += 1
        price = Decimal(r["price"])
        count = int(Decimal(r["count"]))
        yv = Decimal(1 if y == "yes" else 0)
        if r["model_prob"]:
            bm_t += (float(r["model_prob"]) - float(yv)) ** 2
            bk_t += (float(price) - float(yv)) ** 2
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
        st = rep.by_strategy.setdefault(r["strategy"], {"n_scored": 0, "n_contested": 0, "brier_model_contested": None, "brier_market_contested": None})
        st["n_candidates"] = st.get("n_candidates", 0) + 1
        st["pnl_cents"] = str(Decimal(st.get("pnl_cents", "0")) + pnl)
        if pnl > 0:
            hits += 1
            st["hits"] = st.get("hits", 0) + 1
    rep.hit_rate = hits / rep.n_candidates if rep.n_candidates else None
    if rep.n_candidates:
        rep.brier_model_at_trade = bm_t / rep.n_candidates
        rep.brier_market_at_trade = bk_t / rep.n_candidates
    rep.caveats = caveats(rep)
    return rep


def activity_stats(storage: Storage, days: float = 7.0, now: float | None = None, since_model_change: bool = True) -> dict[str, Any]:
    """How often the bot would have traded: distinct market/day pairs that reached the execute stage
    (observed, or filled in trade mode), per day and per week, over the window actually covered."""
    now = time.time() if now is None else now
    since = now - days * 86400
    if since_model_change:
        since = max(since, model_version_since(storage, now))
    rows = storage.execute_decisions(since)
    if not rows:
        return {"window_days": days, "days_observed": 0.0, "trades_per_day": 0.0, "trades_per_week": 0.0, "distinct_positions": 0, "by_strategy": {}}
    first = min(r["ts"] for r in rows)
    covered = max(1 / 24, (now - first) / 86400)  # at least an hour, never divide by zero
    pairs = {(r["market_ticker"], time.strftime("%Y-%m-%d", time.gmtime(r["ts"]))) for r in rows}
    by: dict[str, set] = {}
    for r in rows:
        by.setdefault(r["strategy"], set()).add((r["market_ticker"], time.strftime("%Y-%m-%d", time.gmtime(r["ts"]))))
    per_day = len(pairs) / covered
    return {"window_days": days, "days_observed": round(covered, 2), "distinct_positions": len(pairs), "trades_per_day": round(per_day, 1),
            "trades_per_week": round(per_day * 7, 1), "by_strategy": {k: len(v) for k, v in by.items()}}


def gap_summary(storage: Storage, days: float = 7.0, min_net_edge_cents: Decimal = Decimal("5"),
                now: float | None = None) -> dict[str, Any]:
    """What the ladder-arbitrage scan actually found.

    Arbitrage is the one strategy whose evidence is not a Brier score: a ladder whose YES asks sum
    below a dollar after fees is mispriced no matter what any model thinks. So the question is only
    whether such gaps existed and cleared the fee threshold, which this answers directly instead of
    leaving it in a JSON dump.
    """
    now = time.time() if now is None else now
    rows = storage.arb_gaps(since=now - days * 86400, limit=100000)
    out: dict[str, Any] = {"days": days, "gaps_logged": len(rows), "threshold_cents": str(min_net_edge_cents),
                           "cleared_threshold": 0, "by_kind": {}, "best_net_cents": None, "events_with_a_clearing_gap": [],
                           "verdict": "no ladder gaps logged at all: nothing to trade on this strategy"}
    if not rows:
        return out
    best = None
    events: set[str] = set()
    for r in rows:
        kind = str(r.get("kind"))
        k = out["by_kind"].setdefault(kind, {"logged": 0, "cleared": 0})
        k["logged"] += 1
        net = dec(r.get("net_edge_cents"))
        if net is None:
            continue
        if best is None or net > best:
            best = net
        if net >= min_net_edge_cents:
            k["cleared"] += 1
            out["cleared_threshold"] += 1
            events.add(str(r.get("event_ticker")))
    out["best_net_cents"] = str(best) if best is not None else None
    out["events_with_a_clearing_gap"] = sorted(events)[:50]
    span = (now - min(float(r["ts"]) for r in rows)) / 86400
    out["days_observed"] = round(max(span, 1 / 24), 2)
    if out["cleared_threshold"]:
        out["verdict"] = (f"{out['cleared_threshold']} of {len(rows)} logged gaps cleared {min_net_edge_cents}c net over "
                          f"{out['days_observed']} days, across {len(events)} events. These are structural, not forecasts: "
                          f"worth checking whether the depth was real before trading them.")
    else:
        out["verdict"] = (f"{len(rows)} gaps logged over {out['days_observed']} days but none cleared {min_net_edge_cents}c "
                          f"net after fees (best was {out['best_net_cents']}c). Arbitrage has found nothing tradeable.")
    return out


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
    if rep.n_candidates and rep.brier_model_at_trade is not None and rep.brier_market_at_trade is not None \
            and rep.brier_model_at_trade >= rep.brier_market_at_trade:
        out.insert(0, f"At the moments it would have traded, the model's Brier ({rep.brier_model_at_trade:.4f}) is NOT better than the price it "
                      f"traded against ({rep.brier_market_at_trade:.4f}) over {rep.n_candidates} candidates: the trades themselves carry no edge.")
    if rep.n_contested >= MIN_CONTESTED and rep.brier_model_contested is not None and rep.brier_market_contested is not None \
            and rep.brier_model_contested >= rep.brier_market_contested:
        out.insert(0, "On contested markets the model's Brier score is NOT better than the market's own prices. Any positive P&L here is noise, not edge.")
    elif rep.n_contested < MIN_CONTESTED:
        out.insert(0, f"Only {rep.n_contested} contested settlements so far; no verdict on edge is possible yet. Near-certain markets scored right prove nothing.")
    return out
