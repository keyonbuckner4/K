"""Backfill: replaying a model over already-settled markets, and the shape guards that keep it honest."""

import asyncio
import time
from decimal import Decimal

import httpx
import pytest

from kalshi_bot.backfill import Candle, backfill_crypto, detect_price_scale, parse_candles, settled_markets
from kalshi_bot.client import KalshiClient
from kalshi_bot.data.history import HistoryFeed, HistorySeries, interval_for_span
from kalshi_bot.errors import DataUnavailable, UnexpectedApiResponse
from kalshi_bot.fees import FeeSchedule
from kalshi_bot.gate import GateConfig
from kalshi_bot.models import Series
from kalshi_bot.ratelimit import SharedRateLimiter
from kalshi_bot.storage import Storage

from helpers import settings

NOW = 1_789_000_000.0          # a fixed "now" so every generated timestamp is deterministic
DAYS = 30.0
SINCE = NOW - DAYS * 86400


# ---- candlestick parsing -------------------------------------------------------------------
def test_price_scale_is_decided_once_per_market_not_per_value():
    cents = [{"end_period_ts": 1, "yes_bid": {"close": 42}, "yes_ask": {"close": 45}},
             {"end_period_ts": 2, "yes_bid": {"close": 100}, "yes_ask": {"close": 100}}]
    assert detect_price_scale(cents) == "cents"
    # a whole-dollar price in the same response must not be read as one cent
    assert [str(c.yes_bid) for c in parse_candles(cents)] == ["0.42", "1"]

    dollars = [{"end_period_ts": 1, "yes_bid": {"close": "0.42"}, "yes_ask": {"close": "0.45"}},
               {"end_period_ts": 2, "yes_bid": {"close": "1"}, "yes_ask": {"close": "1"}}]
    assert detect_price_scale(dollars) == "dollars"
    assert [str(c.yes_bid) for c in parse_candles(dollars)] == ["0.42", "1"]

    explicit = [{"end_period_ts": 1, "yes_bid": {"close_dollars": "0.42"}, "yes_ask": {"close_dollars": "0.45"}}]
    assert detect_price_scale(explicit) == "dollars" and parse_candles(explicit)[0].yes_ask == Decimal("0.45")


def test_unknown_candlestick_shapes_raise_rather_than_guess():
    with pytest.raises(UnexpectedApiResponse, match="timestamp"):
        parse_candles([{"yes_bid": {"close": 42}}])
    with pytest.raises(UnexpectedApiResponse, match="no price values"):
        detect_price_scale([{"end_period_ts": 1, "volume": 0}])
    with pytest.raises(UnexpectedApiResponse, match="outside 0-1"):
        parse_candles([{"end_period_ts": 1, "price": {"close": 420}}])
    assert parse_candles([]) == []


def test_market_prob_prefers_the_midpoint_and_falls_back_to_the_traded_price():
    assert Candle(1, Decimal("0.40"), Decimal("0.44")).market_prob() == Decimal("0.42")
    assert Candle(1, price=Decimal("0.37")).market_prob() == Decimal("0.37")
    assert Candle(1).market_prob() is None


# ---- history -------------------------------------------------------------------------------
def test_history_answers_as_of_and_refuses_to_interpolate_a_gap():
    s = HistorySeries("t", times=[100.0, 200.0, 900.0], values=[1.0, 2.0, 3.0], interval_sec=100.0)
    assert s.at(250.0) == 2.0 and s.at(200.0) == 2.0 and s.at(1000.0) == 3.0
    with pytest.raises(DataUnavailable, match="no sample at or before"):
        s.at(50.0)
    with pytest.raises(DataUnavailable, match="not interpolating"):
        s.at(800.0)   # 600s after the last sample, past the 300s staleness limit


def test_realized_vol_needs_a_real_window():
    hourly = HistorySeries("t", times=[float(i * 3600) for i in range(200)],
                           values=[100.0 * (1 + 0.001 * ((i % 7) - 3)) for i in range(200)], interval_sec=3600.0)
    assert hourly.realized_vol_at(150 * 3600, window_hours=72) > 0
    with pytest.raises(DataUnavailable, match="need 30"):
        hourly.realized_vol_at(10 * 3600, window_hours=5)


def test_interval_covers_the_span_in_one_response():
    assert interval_for_span(30 * 86400) == 60      # 720 hourly candles is exactly 30 days
    assert interval_for_span(2 * 86400) == 5
    with pytest.raises(DataUnavailable, match="exceeds"):
        interval_for_span(400 * 365 * 86400)


# ---- end to end ----------------------------------------------------------------------------
SPOT = 100_000.0
SERIES = {"ticker": "KXBTCD", "title": "BTC daily", "category": "Crypto", "fee_type": "quadratic", "fee_multiplier": "0.07"}


def make_markets(n=25, floor=95_000):
    """n daily 'BTC above floor' markets, one per day, every one settled YES (spot never fell)."""
    out = []
    for i in range(n):
        settle = SINCE + (i + 1) * 86400
        out.append({"ticker": f"KXBTCD-D{i}-T{floor}", "event_ticker": f"KXBTCD-D{i}", "status": "finalized",
                    "close_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(settle)),
                    "expiration_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(settle)),
                    "category": "Crypto", "strike_type": "greater", "floor_strike": str(floor), "result": "yes"})
    return out


def kalshi_handler(markets, candle_price=("0.60", "0.62")):
    def handler(req: httpx.Request):
        path = req.url.path
        if path.endswith("/candlesticks"):
            ticker = path.split("/markets/")[1].split("/")[0]
            end = int(req.url.params["end_ts"])
            bid, ask = candle_price
            # hourly candles over the day before settlement
            items = [{"end_period_ts": end - h * 3600, "yes_bid": {"close_dollars": bid}, "yes_ask": {"close_dollars": ask},
                      "price": {"close_dollars": bid}, "volume": 10} for h in range(1, 25)]
            assert ticker
            return httpx.Response(200, json={"candlesticks": items})
        if path.endswith("/markets"):
            return httpx.Response(200, json={"markets": markets, "cursor": ""})
        if "/series/" in path:
            return httpx.Response(200, json={"series": SERIES})
        return httpx.Response(404, json={"error": {"message": f"unrouted {path}"}})
    return handler


def history_handler(dvol_status=200):
    def handler(req: httpx.Request):
        if req.url.host == "api.kraken.com":
            n = 720
            rows = [[SINCE + i * 3600, "0", "0", "0", f"{SPOT * (1 + 0.0005 * ((i % 5) - 2)):.2f}", "0", "0", 0] for i in range(n)]
            return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": rows, "last": 0}})
        if req.url.host == "www.deribit.com":
            if dvol_status != 200:
                return httpx.Response(dvol_status, json={})
            rows = [[int((SINCE + i * 3600) * 1000), 50.0, 50.0, 50.0, 50.0] for i in range(720)]
            return httpx.Response(200, json={"result": {"data": rows, "continuation": None}})
        return httpx.Response(404)
    return handler


def run_backfill(tmp_path, markets, candle_price=("0.60", "0.62"), dvol_status=200, vol_source="deribit_dvol"):
    st = settings(tmp_path)
    client = KalshiClient(st, SharedRateLimiter.from_config(None), None, transport=httpx.MockTransport(kalshi_handler(markets, candle_price)))
    history = HistoryFeed(transport=httpx.MockTransport(history_handler(dvol_status)))
    scratch = Storage(tmp_path / "backfill.db", log_heartbeat_sec=0)
    fees = FeeSchedule()
    fees.register_series(Series.parse(SERIES))

    async def go():
        try:
            return await backfill_crypto(client, history, scratch, series_map={"BTC": "KXBTCD"}, days=DAYS,
                                         gate=GateConfig(), fee_sched=fees, max_contracts=5, vol_source=vol_source, now=NOW)
        finally:
            await client.close()
            await history.close()

    rep, stats = asyncio.run(go())
    scratch.close()
    return rep, stats


def test_backfill_scores_settled_markets_and_finds_the_edge(tmp_path):
    """Spot sat at 100k all month, so 'above 95k' settled YES every day; the market charged 62c for it.
    The replayed model should price those near certain, take the cheap YES, and beat the market's Brier."""
    rep, stats = run_backfill(tmp_path, make_markets(25))
    assert stats.markets_seen == 25 and stats.markets_scored == 25
    assert stats.decision_points > 25 and stats.candidates == 25
    assert stats.vol_source == "deribit_dvol" and stats.price_scale == "dollars"
    assert rep.n_scored == 25 and rep.n_contested == 25
    assert rep.brier_model_contested < rep.brier_market_contested
    assert rep.verdict.startswith("model beats")
    # every candidate bought YES at the 62c ask and YES happened, so the pessimistic P&L is positive
    assert rep.n_candidates == 25 and rep.hit_rate == 1.0 and rep.pnl_cents > 0
    assert rep.brier_model_at_trade < rep.brier_market_at_trade
    assert rep.caveats[0].startswith("BACKFILL") and "no order book" in rep.caveats[0]


def test_backfill_takes_no_candidate_when_the_market_is_already_near_certain(tmp_path):
    """The same markets priced at 97c/98c: the model agrees, and gate rule 5 forbids betting either way."""
    rep, stats = run_backfill(tmp_path, make_markets(25), candle_price=("0.97", "0.98"))
    assert stats.candidates == 0 and rep.n_candidates == 0
    assert rep.n_scored == 25 and rep.n_contested == 0     # 97c-98c is outside the 5c-95c contested band
    assert rep.verdict.startswith("insufficient evidence")


def test_backfill_falls_back_to_realized_vol_and_says_so(tmp_path):
    rep, stats = run_backfill(tmp_path, make_markets(5), dvol_status=503)
    assert "DVOL history unavailable" in stats.vol_source
    assert any("Volatility input: realized" in c for c in rep.caveats)


def test_backfill_ignores_markets_that_have_not_settled(tmp_path):
    markets = make_markets(5)
    for m in markets[:2]:
        m["result"] = ""
    rep, stats = run_backfill(tmp_path, markets)
    assert stats.markets_seen == 3 and rep.n_scored == 3


def test_settled_markets_does_not_filter_on_a_guessed_status(tmp_path):
    """Kalshi's vocabulary for finished markets is not guessed at: the result field decides."""
    markets = make_markets(3)
    markets[0]["status"] = "closed"
    markets[1]["status"] = "settled"
    markets[2]["status"] = "finalized"
    st = settings(tmp_path)
    client = KalshiClient(st, SharedRateLimiter.from_config(None), None, transport=httpx.MockTransport(kalshi_handler(markets)))

    async def go():
        try:
            return await settled_markets(client, "KXBTCD", SINCE)
        finally:
            await client.close()

    assert len(asyncio.run(go())) == 3
