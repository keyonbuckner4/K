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
# hourly forecast (Celsius) for Sep 10 local 06:00-23:00 (10:00Z-03:00Z) peaking at 25C mid-afternoon, and Sep 11 peaking at 28C
_HOURLY = [(f"2026-09-10T{h:02d}:00:00+00:00", c) for h, c in zip(range(10, 24), [18, 19, 20, 21, 22, 23, 24, 25, 25, 24, 23, 22, 21, 20])]
_HOURLY += [(f"2026-09-11T{h:02d}:00:00+00:00", c) for h, c in zip(range(0, 4), [19, 18, 18, 17])]
_HOURLY += [(f"2026-09-11T{h:02d}:00:00+00:00", c) for h, c in zip(range(10, 24), [19, 20, 22, 24, 26, 27, 28, 28, 27, 26, 24, 23, 22, 21])]
NWS_GRID = {"properties": {
    "updateTime": "2026-09-10T10:00:00+00:00",
    "temperature": {"values": [{"validTime": f"{t}/PT1H", "value": c} for t, c in _HOURLY]},
    "maxTemperature": {"values": [{"validTime": "2026-09-10T11:00:00+00:00/PT13H", "value": 25.0}, {"validTime": "2026-09-11T11:00:00+00:00/PT13H", "value": 28.0}]},
    "minTemperature": {"values": [{"validTime": "2026-09-10T23:00:00+00:00/PT14H", "value": 15.0}]},
    "probabilityOfPrecipitation": {"values": [{"validTime": "2026-09-10T12:00:00+00:00/PT6H", "value": 40}]},
}}
# station observations on Sep 10 (UTC timestamps; 09:51Z is 05:51 EDT): the morning climbs to 23C = 73.4F by 08:51 local
NWS_OBS = {"features": [{"properties": {"timestamp": f"2026-09-10T{h:02d}:51:00+00:00", "temperature": {"value": c}}}
                        for h, c in [(9, 20.0), (10, 21.0), (11, 22.0), (12, 23.0)]]
           + [{"properties": {"timestamp": "2026-09-10T08:51:00+00:00", "temperature": {"value": None}}}]}


def nws_handler(req: httpx.Request):
    if req.url.path.startswith("/points/"):
        assert "User-Agent" in req.headers
        return httpx.Response(200, json=NWS_POINTS)
    if req.url.path.startswith("/gridpoints/OKX/33,37"):
        return httpx.Response(200, json=NWS_GRID)
    if req.url.path == "/stations/KNYC/observations":
        return httpx.Response(200, json=NWS_OBS)
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
    c = ScanContext(NOW - timedelta(hours=2), [ev], books, fees("KXHIGHNY"), GateConfig(), storage)   # 09:00 local: inside the trading window
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
    fc = json.loads(storage.conn.execute("SELECT payload FROM forecasts").fetchone()[0])
    assert fc["floor_f"] == 73.0 and fc["hours_left"] == 14 and fc["candidates_allowed"] is True and abs(fc["mean_f"] - 77.0) < 1e-9


def test_weather_afternoon_uses_observed_floor_and_suppresses_candidates(tmp_path):
    """At 15:00 local the station has already hit 79F: buckets below 79 are impossible, the top of the
    ladder is priced from the remaining hours, and no trade is proposed after the morning window."""
    hot_obs = {"features": [{"properties": {"timestamp": f"2026-09-10T{h:02d}:51:00+00:00", "temperature": {"value": c}}}
                            for h, c in [(12, 23.0), (15, 25.0), (17, 26.1), (18, 25.5)]]}   # 26.1C = 79.0F at 13:51 local

    def handler(req: httpx.Request):
        if req.url.path == "/stations/KNYC/observations":
            return httpx.Response(200, json=hot_obs)
        return nws_handler(req)

    ms = [market(ticker="KXHIGHNY-26SEP10-T75", strike_type="less_or_equal", floor=None, cap="75", yes_bid="0.03", yes_ask="0.05"),
          market(ticker="KXHIGHNY-26SEP10-B76", strike_type="between", floor="76", cap="77", yes_bid="0.10", yes_ask="0.12"),
          market(ticker="KXHIGHNY-26SEP10-B78", strike_type="between", floor="78", cap="79", yes_bid="0.60", yes_ask="0.62"),
          market(ticker="KXHIGHNY-26SEP10-T80", strike_type="greater_or_equal", floor="80", cap=None, yes_bid="0.20", yes_ask="0.22")]
    ev = Event.parse({"event_ticker": "KXHIGHNY-26SEP10", "series_ticker": "KXHIGHNY", "mutually_exclusive": True, "category": "Climate and Weather",
                      "markets": [m.raw for m in ms]})
    books = {m.ticker: book(m.ticker, m.raw["yes_bid_dollars"], m.raw["yes_ask_dollars"]) for m in ms}
    storage = Storage(tmp_path / "x.db")
    strat = WeatherStrategy(weather_cfg(), storage, nws=NWSClient("t", transport=httpx.MockTransport(handler)))
    c = ScanContext(NOW + timedelta(hours=4), [ev], books, fees("KXHIGHNY"), GateConfig(), storage)   # 15:00 local
    intents = asyncio.run(strat.scan(c))
    assert intents == []   # after the morning window nothing is proposed, even though the 76-77 bucket at 12c is "free money" for the model
    rows = {r["market_ticker"]: r for r in storage.decisions(strategy="weather") if r["stage"] == "model"}
    assert Decimal(rows["KXHIGHNY-26SEP10-T75"]["model_prob"]) == 0 and Decimal(rows["KXHIGHNY-26SEP10-B76"]["model_prob"]) == 0
    p78 = float(rows["KXHIGHNY-26SEP10-B78"]["model_prob"])
    p80 = float(rows["KXHIGHNY-26SEP10-T80"]["model_prob"])
    assert abs(p78 + p80 - 1.0) < 1e-6 and p78 > 0.6 and p80 > 0.2   # at least 79; one more degree stays possible (reporting noise)
    suppressed = [r for r in storage.decisions(strategy="weather") if r["stage"] == "window"]
    assert suppressed and "suppressed" in suppressed[0]["reason"]
    fc = json.loads(storage.conn.execute("SELECT payload FROM forecasts").fetchone()[0])
    assert fc["floor_f"] == 79.0 and fc["candidates_allowed"] is False and fc["hours_left"] == 8


def test_weather_future_day_uses_the_plain_forecast(tmp_path):
    ms = [market(ticker="KXHIGHNY-26SEP11-B82", strike_type="between", floor="82", cap="83", yes_bid="0.20", yes_ask="0.22")]
    ev = Event.parse({"event_ticker": "KXHIGHNY-26SEP11", "series_ticker": "KXHIGHNY", "mutually_exclusive": True, "markets": [m.raw for m in ms]})
    books = {m.ticker: book(m.ticker, "0.20", "0.22") for m in ms}
    storage = Storage(tmp_path / "x.db")
    strat = WeatherStrategy(weather_cfg(), storage, nws=NWSClient("t", transport=httpx.MockTransport(nws_handler)))
    intents = asyncio.run(strat.scan(ScanContext(NOW, [ev], books, fees("KXHIGHNY"), GateConfig(), storage)))
    assert intents and intents[0].legs[0].book_side == "bid"   # forecast 82.4F: the 82-83 bucket at 22c is cheap
    fc = json.loads(storage.conn.execute("SELECT payload FROM forecasts").fetchone()[0])
    assert fc["floor_f"] is None and fc["cap_f"] is None and fc["candidates_allowed"] is True and fc["sigma_f"] > 2.0


def test_local_standard_day_ignores_daylight_saving():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from kalshi_bot.data.nws import local_standard_day

    tz = ZoneInfo("America/New_York")
    assert local_standard_day(datetime(2026, 9, 11, 4, 30, tzinfo=timezone.utc), tz) == "2026-09-10"   # 00:30 EDT is still Sep 10 in EST
    assert local_standard_day(datetime(2026, 9, 11, 5, 30, tzinfo=timezone.utc), tz) == "2026-09-11"


def test_weather_without_forecast_rejects_not_guesses(tmp_path):
    ev = Event.parse({"event_ticker": "KXHIGHNY-26OCT30", "series_ticker": "KXHIGHNY", "mutually_exclusive": True,
                      "markets": [market(ticker="KXHIGHNY-26OCT30-B76", strike_type="between", floor="76", cap="77").raw]})
    storage = Storage(tmp_path / "x.db")
    strat = WeatherStrategy(weather_cfg(), storage, nws=NWSClient("t", transport=httpx.MockTransport(nws_handler)))
    c = ScanContext(NOW, [ev], {}, fees("KXHIGHNY"), GateConfig(), storage)
    assert asyncio.run(strat.scan(c)) == []
    assert "no high temperature" in storage.decisions()[0]["reason"]


# ---- crypto -----------------------------------------------------------------------------------
SEEN_KRAKEN = {}


def kraken_handler(req: httpx.Request):
    if req.url.path == "/0/public/Ticker":
        return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": {"c": ["100000.0", "1.0"]}}})
    if req.url.path == "/0/public/OHLC":
        SEEN_KRAKEN["interval"] = int(req.url.params["interval"])
        n = min(720, int(72 * 60 / SEEN_KRAKEN["interval"]))  # Kraken caps responses at 720 rows
        closes = [100000 * (1 + 0.001 * ((i % 7) - 3)) for i in range(n)]
        return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": [[0, "0", "0", "0", str(c), "0", "0", 0] for c in closes], "last": 0}})
    return httpx.Response(404)


def test_realized_vol_and_feed():
    from kalshi_bot.data.crypto_feed import kraken_interval_for

    with pytest.raises(DataUnavailable):
        realized_vol_annualized([1.0] * 5, 1)
    assert kraken_interval_for(72) == 15 and kraken_interval_for(12) == 1 and kraken_interval_for(24) == 5
    feed = CryptoFeed(transport=httpx.MockTransport(kraken_handler), window_hours=72)
    q = asyncio.run(feed.quote("BTC"))
    assert SEEN_KRAKEN["interval"] == 15  # 72h of 1-minute candles would exceed Kraken's 720-row cap
    assert q.spot == 100000.0 and q.sigma_annual > 0 and q.source == "kraken_spot+realized_72h@15m"


def test_realized_vol_refuses_a_truncated_window():
    def short_handler(req: httpx.Request):
        if req.url.path == "/0/public/Ticker":
            return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": {"c": ["100000.0", "1.0"]}}})
        closes = [100000 + i for i in range(40)]  # only 40 x 15m = 10h of a 72h window
        return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": [[0, "0", "0", "0", str(c), "0", "0", 0] for c in closes], "last": 0}})

    feed = CryptoFeed(transport=httpx.MockTransport(short_handler), window_hours=72)
    with pytest.raises(DataUnavailable, match="under half"):
        asyncio.run(feed.quote("BTC"))


def test_crypto_feed_prefers_dvol_and_falls_back_to_realized(caplog):
    def with_deribit(status):
        def handler(req: httpx.Request):
            if req.url.host == "www.deribit.com":
                return httpx.Response(status, json={"result": {"index_price": 55.0}} if status == 200 else {})
            return kraken_handler(req)
        return handler

    q = asyncio.run(CryptoFeed(vol_source="deribit_dvol", transport=httpx.MockTransport(with_deribit(200))).quote("BTC"))
    assert q.sigma_annual == 0.55 and q.source == "kraken_spot+deribit_dvol"
    q2 = asyncio.run(CryptoFeed(vol_source="deribit_dvol", transport=httpx.MockTransport(with_deribit(503))).quote("BTC"))
    assert "dvol_fallback" in q2.source and q2.sigma_annual > 0 and "Deribit DVOL unavailable" in caplog.text
    with pytest.raises(DataUnavailable):
        asyncio.run(CryptoFeed(vol_source="deribit_dvol", fallback_realized=False, transport=httpx.MockTransport(with_deribit(503))).quote("BTC"))


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
