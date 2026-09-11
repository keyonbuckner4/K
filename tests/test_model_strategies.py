import asyncio
import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from kalshi_bot.data.crypto_feed import CryptoFeed, realized_vol_annualized
from kalshi_bot.data.nws import NWSClient, parse_duration
from kalshi_bot.errors import DataUnavailable
from kalshi_bot.fees import FeeSchedule
from kalshi_bot.gate import GateConfig
from kalshi_bot.models import Event, Series
from kalshi_bot.orderbook import OrderBook
from kalshi_bot.storage import Storage
from kalshi_bot.strategies.base import ScanContext
from kalshi_bot.strategies.crypto import CryptoThresholdStrategy
from kalshi_bot.strategies.economics import EconomicsStrategy
from kalshi_bot.strategies.weather import WeatherStrategy, event_date

from helpers import NOW, market


def fees(*tickers):
    f = FeeSchedule()
    for t in tickers:
        f.register_series(Series.parse({"ticker": t, "fee_type": "quadratic", "fee_multiplier": "0.07"}))
    return f


def book(ticker, bid, ask, qty="100"):
    return OrderBook.from_payload(ticker, {"orderbook_fp": {"yes_dollars": [[bid, qty]], "no_dollars": [[str(round(1 - float(ask), 4)), qty]]}})


# ---- NWS -------------------------------------------------------------------------------------
NWS_POINTS = {"properties": {"gridId": "OKX", "gridX": 33, "gridY": 37, "timeZone": "America/New_York"}}
NWS_GRID = {"properties": {
    "updateTime": "2026-09-10T10:00:00+00:00",
    "maxTemperature": {"values": [{"validTime": "2026-09-10T11:00:00+00:00/PT13H", "value": 25.0}, {"validTime": "2026-09-11T11:00:00+00:00/PT13H", "value": 28.0}]},
    "minTemperature": {"values": [{"validTime": "2026-09-10T23:00:00+00:00/PT14H", "value": 15.0}]},
    "probabilityOfPrecipitation": {"values": [{"validTime": "2026-09-10T12:00:00+00:00/PT6H", "value": 40}]},
}}


def nws_handler(req: httpx.Request):
    if req.url.path.startswith("/points/"):
        assert "User-Agent" in req.headers
        return httpx.Response(200, json=NWS_POINTS)
    if req.url.path.startswith("/gridpoints/OKX/33,37"):
        return httpx.Response(200, json=NWS_GRID)
    return httpx.Response(404)


def test_nws_daily_parsing():
    c = NWSClient("test (test@example.com)", transport=httpx.MockTransport(nws_handler))
    d = asyncio.run(c.daily(40.7789, -73.9692))
    assert abs(d["2026-09-10"].high_f - 77.0) < 1e-9 and abs(d["2026-09-11"].high_f - 82.4) < 1e-9
    assert abs(d["2026-09-11"].low_f - 59.0) < 1e-9  # min period ending the morning of the 11th
    assert d["2026-09-10"].pop == 0.4
    assert parse_duration("P1DT2H30M") == timedelta(days=1, hours=2, minutes=30)


def test_nws_failure_is_data_unavailable():
    c = NWSClient("t", transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(DataUnavailable):
        asyncio.run(c.daily(1, 2))


def test_event_date_from_ticker_and_strike_date():
    assert event_date(Event.parse({"event_ticker": "KXHIGHNY-26SEP10"})) == "2026-09-10"
    assert event_date(Event.parse({"event_ticker": "KXHIGHNY-26SEP10", "strike_date": "2026-09-11T05:00:00Z"})) == "2026-09-10"  # ticker wins
    assert event_date(Event.parse({"event_ticker": "KXWEIRD", "strike_date": "2026-09-11T02:00:00Z"})) == "2026-09-10"  # 10pm ET the day before
    assert event_date(Event.parse({"event_ticker": "KXWEIRD"})) is None


def weather_cfg():
    return {"enabled": True, "mode": "observe", "max_contracts": 5, "sigma_f_day0": 2.0, "sigma_f_per_day": 0.5,
            "cities": [{"name": "NYC", "series_high": "KXHIGHNY", "series_low": "KXLOWNY", "station": "KNYC", "lat": 40.7789, "lon": -73.9692}]}


def test_weather_strategy_prices_ladder_and_emits_edge(tmp_path):
    # forecast high 77F (25C). Bucket 76-77 should be likely; market prices it cheap.
    ms = [market(ticker="KXHIGHNY-26SEP10-T75", strike_type="less_or_equal", floor=None, cap="75", yes_bid="0.20", yes_ask="0.22"),
          market(ticker="KXHIGHNY-26SEP10-B76", strike_type="between", floor="76", cap="77", yes_bid="0.20", yes_ask="0.22"),
          market(ticker="KXHIGHNY-26SEP10-T78", strike_type="greater_or_equal", floor="78", cap=None, yes_bid="0.50", yes_ask="0.52")]
    ev = Event.parse({"event_ticker": "KXHIGHNY-26SEP10", "series_ticker": "KXHIGHNY", "mutually_exclusive": True, "category": "Climate and Weather",
                      "markets": [m.raw for m in ms]})
    books = {m.ticker: book(m.ticker, m.raw["yes_bid_dollars"], m.raw["yes_ask_dollars"]) for m in ms}
    storage = Storage(tmp_path / "x.db")
    nws = NWSClient("t", transport=httpx.MockTransport(nws_handler))
    strat = WeatherStrategy(weather_cfg(), storage, nws=nws)
    assert set(strat.series()) == {"KXHIGHNY", "KXLOWNY"}
    c = ScanContext(NOW, [ev], books, fees("KXHIGHNY"), GateConfig(), storage)
    intents = asyncio.run(strat.scan(c))
    tickers = {i.legs[0].ticker: i for i in intents}
    assert "KXHIGHNY-26SEP10-B76" in tickers
    b76 = tickers["KXHIGHNY-26SEP10-B76"].legs[0]
    assert b76.book_side == "bid" and b76.model_prob > Decimal("0.35")
    assert "KXHIGHNY-26SEP10-T78" in tickers and tickers["KXHIGHNY-26SEP10-T78"].legs[0].book_side == "ask"  # market too high -> buy NO
    # every priced market has a logged model decision with its probability, and the forecast is stored
    rows = storage.decisions(strategy="weather")
    assert {r["market_ticker"] for r in rows if r["model_prob"]} == {m.ticker for m in ms}
    assert storage.conn.execute("SELECT count(*) FROM forecasts").fetchone()[0] == 1


def test_weather_without_forecast_rejects_not_guesses(tmp_path):
    ev = Event.parse({"event_ticker": "KXHIGHNY-26OCT30", "series_ticker": "KXHIGHNY", "mutually_exclusive": True,
                      "markets": [market(ticker="KXHIGHNY-26OCT30-B76", strike_type="between", floor="76", cap="77").raw]})
    storage = Storage(tmp_path / "x.db")
    strat = WeatherStrategy(weather_cfg(), storage, nws=NWSClient("t", transport=httpx.MockTransport(nws_handler)))
    c = ScanContext(NOW, [ev], {}, fees("KXHIGHNY"), GateConfig(), storage)
    assert asyncio.run(strat.scan(c)) == []
    assert "no high temperature" in storage.decisions()[0]["reason"]


# ---- crypto -----------------------------------------------------------------------------------
def kraken_handler(req: httpx.Request):
    if req.url.path == "/0/public/Ticker":
        return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": {"c": ["100000.0", "1.0"]}}})
    if req.url.path == "/0/public/OHLC":
        closes = [100000 * (1 + 0.001 * ((i % 7) - 3)) for i in range(200)]
        return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": [[0, "0", "0", "0", str(c), "0", "0", 0] for c in closes], "last": 0}})
    return httpx.Response(404)


def test_realized_vol_and_feed():
    with pytest.raises(DataUnavailable):
        realized_vol_annualized([1.0] * 5, 1)
    feed = CryptoFeed(transport=httpx.MockTransport(kraken_handler))
    q = asyncio.run(feed.quote("BTC"))
    assert q.spot == 100000.0 and q.sigma_annual > 0 and "realized" in q.source


def test_crypto_strategy_prices_thresholds(tmp_path):
    close = NOW + timedelta(hours=1)
    ms = [market(ticker="KXBTCD-26SEP10H16-T95000", event="KXBTCD-26SEP10H16", strike_type="greater", floor="95000", cap=None, close=close,
                 category="Crypto", yes_bid="0.60", yes_ask="0.62"),
          market(ticker="KXBTCD-26SEP10H16-T105000", event="KXBTCD-26SEP10H16", strike_type="greater", floor="105000", cap=None, close=close,
                 category="Crypto", yes_bid="0.30", yes_ask="0.32")]
    ev = Event.parse({"event_ticker": "KXBTCD-26SEP10H16", "series_ticker": "KXBTCD", "mutually_exclusive": False, "category": "Crypto", "markets": [m.raw for m in ms]})
    books = {m.ticker: book(m.ticker, m.raw["yes_bid_dollars"], m.raw["yes_ask_dollars"]) for m in ms}
    storage = Storage(tmp_path / "x.db")
    strat = CryptoThresholdStrategy({"series": {"BTC": "KXBTCD"}, "max_contracts": 5, "min_minutes_to_close": 20}, storage,
                                    feed=CryptoFeed(transport=httpx.MockTransport(kraken_handler)))
    c = ScanContext(NOW, [ev], books, fees("KXBTCD"), GateConfig(), storage)
    intents = asyncio.run(strat.scan(c))
    by = {i.legs[0].ticker: i.legs[0] for i in intents}
    # spot 100k, 5% away in one hour with low realized vol: above-95k is ~certain (market 62c -> buy YES), above-105k ~impossible (market 30c -> buy NO)
    assert by["KXBTCD-26SEP10H16-T95000"].book_side == "bid" and by["KXBTCD-26SEP10H16-T105000"].book_side == "ask"
    # too-close-to-expiry markets are rejected with a reason
    soon = market(ticker="KXBTCD-26SEP10H15-T95000", event="KXBTCD-26SEP10H15", strike_type="greater", floor="95000", cap=None, close=NOW + timedelta(minutes=10), category="Crypto")
    ev2 = Event.parse({"event_ticker": "KXBTCD-26SEP10H15", "series_ticker": "KXBTCD", "markets": [soon.raw]})
    c2 = ScanContext(NOW, [ev2], {soon.ticker: book(soon.ticker, "0.5", "0.52")}, fees("KXBTCD"), GateConfig(), storage)
    assert asyncio.run(strat.scan(c2)) == []
    assert any("closes in" in d["reason"] for d in storage.decisions())


# ---- economics -----------------------------------------------------------------------------------
def test_economics_requires_a_view(tmp_path):
    views = tmp_path / "views.toml"
    views.write_text('[[views]]\nevent_ticker = "KXCPI-26SEP"\nmean = 0.3\nsd = 0.1\n')
    ms = [market(ticker="KXCPI-26SEP-T0.4", event="KXCPI-26SEP", strike_type="greater", floor="0.4", cap=None, category="Economics", yes_bid="0.40", yes_ask="0.42"),
          market(ticker="KXCPI-26SEP-B0.2", event="KXCPI-26SEP", strike_type="between", floor="0.2", cap="0.3", category="Economics", yes_bid="0.20", yes_ask="0.22")]
    ev = Event.parse({"event_ticker": "KXCPI-26SEP", "series_ticker": "KXCPI", "category": "Economics", "markets": [m.raw for m in ms]})
    other = Event.parse({"event_ticker": "KXCPI-26OCT", "series_ticker": "KXCPI", "markets": [market(ticker="KXCPI-26OCT-T0.4", event="KXCPI-26OCT", strike_type="greater", floor="0.4", cap=None).raw]})
    storage = Storage(tmp_path / "x.db")
    strat = EconomicsStrategy({"views_path": str(views)}, storage)
    assert strat.series() == ["KXCPI"]
    books = {m.ticker: book(m.ticker, m.raw["yes_bid_dollars"], m.raw["yes_ask_dollars"]) for m in ms}
    c = ScanContext(NOW, [ev, other], books, fees("KXCPI"), GateConfig(), storage)
    intents = asyncio.run(strat.scan(c))
    sides = {i.legs[0].ticker: i.legs[0].book_side for i in intents}
    assert sides.get("KXCPI-26SEP-T0.4") == "ask"   # P(X>0.4 | N(0.3,0.1)) = 16% vs market 42c -> buy NO
    assert sides.get("KXCPI-26SEP-B0.2") == "bid"   # P(0.2<=X<=0.3) = 34% vs market 22c -> buy YES
    assert any("no consensus view" in d["reason"] for d in storage.decisions())
