"""Series discovery: finding what Kalshi actually lists, and letting strategies adopt it.

Ticker names for the intraday crypto, oil and economic ladders are not something to guess, so the
bot reads them off the exchange and matches by ticker prefix or event category instead.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_bot.config import load_settings
from kalshi_bot.data.crypto_feed import CryptoFeed
from kalshi_bot.discover import configured_series, profile_series
from kalshi_bot.engine import Engine
from kalshi_bot.models import Event
from kalshi_bot.strategies.crypto import CryptoThresholdStrategy
from kalshi_bot.strategies.ladder_arb import LadderArbStrategy
from kalshi_bot.storage import Storage

from fake_exchange import FakeKalshi
from test_model_strategies import kraken_handler

NOW = datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc)
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def ev(series, ticker, minutes, category="Crypto", title="", n=3, me=True):
    close = (NOW + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    markets = [{"ticker": f"{ticker}-T{i}", "event_ticker": ticker, "status": "active", "close_time": close,
                "category": category, "strike_type": "greater", "floor_strike": str(100 + i)} for i in range(n)]
    return Event.parse({"event_ticker": ticker, "series_ticker": series, "category": category, "title": title,
                        "mutually_exclusive": me, "markets": markets})


def test_profiles_group_by_series_and_classify_the_horizon():
    events = [ev("KXBTC", "KXBTC-26SEP15H15", 12, title="Bitcoin above at 3:15pm"),
              ev("KXBTC", "KXBTC-26SEP15H16", 27, title="Bitcoin above at 3:30pm"),
              ev("KXBTCD", "KXBTCD-26SEP15", 9 * 60, title="Bitcoin daily"),
              ev("KXOIL", "KXOIL-26SEP30", 15 * 24 * 60, category="Commodities", title="WTI crude settle")]
    ps = {p.series_ticker: p for p in profile_series(events, NOW)}
    assert ps["KXBTC"].events == 2 and ps["KXBTC"].markets == 6
    assert ps["KXBTC"].horizon == "intraday (<=30 min)" and ps["KXBTC"].min_minutes_to_close == 12
    assert ps["KXBTCD"].horizon == "daily" and ps["KXOIL"].horizon == "long-dated"
    assert ps["KXOIL"].category == "Commodities" and ps["KXOIL"].mutually_exclusive is True
    assert ps["KXOIL"].to_dict()["ladder"] is True
    assert profile_series(events, NOW)[0].markets >= profile_series(events, NOW)[-1].markets   # biggest first
    assert ps["KXBTC"].to_dict()["used_by"] == ["(not configured)"]


def test_configured_series_reads_every_strategy_out_of_the_toml():
    toml = {"strategies": {"ladder_arb": {"series": ["KXBTCD"]},
                           "weather": {"cities": [{"series_high": "KXHIGHNY", "series_low": "KXLOWNY"}]},
                           "crypto": {"series": {"BTC": "KXBTCD", "ETH": "KXETHD"}}}}
    got = configured_series(toml)
    assert sorted(got["KXBTCD"]) == ["crypto", "ladder_arb"] and got["KXHIGHNY"] == ["weather"]
    assert got["KXETHD"] == ["crypto"] and "KXOIL" not in got


def test_ladder_arb_adopts_by_category_because_oil_tickers_cannot_be_guessed(tmp_path):
    s = Storage(tmp_path / "x.db")
    strat = LadderArbStrategy({"enabled": True, "series": ["KXBTCD"],
                               "categories": ["commodit", "econom"]}, s)
    profiles = profile_series([ev("KXOIL", "KXOIL-26SEP30", 999, category="Commodities", title="WTI crude"),
                               ev("KXCPI", "KXCPI-26OCT", 9999, category="Economics", title="CPI year over year"),
                               ev("KXNBA", "KXNBA-1", 200, category="Sports", title="game")], NOW)
    assert {p.series_ticker for p in profiles if strat.wants(p)} == {"KXOIL", "KXCPI"}
    added = strat.adopt_series(profiles)
    assert set(added) == {"KXOIL", "KXCPI"}          # sport is not in the categories
    assert set(strat.series()) == {"KXBTCD", "KXOIL", "KXCPI"}
    assert strat.adopt_series(profiles) == []        # idempotent
    s.close()


def test_crypto_adopts_intraday_series_by_prefix_and_keeps_the_asset(tmp_path):
    s = Storage(tmp_path / "x.db")
    strat = CryptoThresholdStrategy({"enabled": True, "series": {"BTC": "KXBTCD", "ETH": "KXETHD"},
                                     "series_patterns": {"BTC": ["KXBTC"], "ETH": ["KXETH"]}}, s,
                                    feed=CryptoFeed(transport=httpx.MockTransport(kraken_handler)))
    profiles = profile_series([ev("KXBTC", "KXBTC-H15", 12), ev("KXETH", "KXETH-H15", 12),
                               ev("KXDOGE", "KXDOGE-1", 12), ev("KXBTCD", "KXBTCD-1", 600)], NOW)
    added = strat.adopt_series(profiles)
    assert set(added) == {"KXBTC", "KXETH"}          # no Kraken pair for DOGE, and KXBTCD is already configured
    assert set(strat.series()) == {"KXBTCD", "KXETHD", "KXBTC", "KXETH"}
    by_series = {v.upper(): k.split(":", 1)[0] for k, v in strat.series_map.items()}
    assert by_series["KXBTC"] == "BTC" and by_series["KXETH"] == "ETH"
    s.close()


def test_volatility_input_is_matched_to_the_horizon():
    """A 15-minute market and a daily one share an underlying but not a horizon: 30-day implied vol
    is the wrong input for the former, so the feed switches to short-window realized vol."""
    def handler(req):
        if req.url.host == "www.deribit.com":
            return httpx.Response(200, json={"result": {"index_price": 55.0}})
        return kraken_handler(req)

    feed = CryptoFeed(vol_source="deribit_dvol", transport=httpx.MockTransport(handler),
                      short_horizon_hours=6, short_window_hours=12)
    assert feed.regime_for(15 * 60) == "short" and feed.regime_for(20 * 3600) == "standard"
    assert feed.regime_for(None) == "standard"

    intraday = asyncio.run(feed.quote("BTC", tau_seconds=15 * 60))
    daily = asyncio.run(feed.quote("BTC", tau_seconds=20 * 3600))
    assert "intraday" in intraday.source and "realized" in intraday.source
    assert daily.source == "kraken_spot+deribit_dvol" and daily.sigma_annual == 0.55
    assert intraday.sigma_annual != daily.sigma_annual   # different inputs, cached separately


TOML = """
[hosts.demo]
rest = "https://demo.test/trade-api/v2"
ws = "wss://demo.test/trade-api/ws/v2"
[storage]
db_path = "data/bot.db"
[engine]
scan_interval_sec = 1
market_cache_ttl_sec = 0
series_discovery_sec = 3600
max_discovered_series = 40
blocked_categories = ["sport"]
[strategies.crypto]
enabled = true
mode = "observe"
series = { BTC = "KXBTCD" }
series_patterns = { BTC = ["KXBTC"] }
min_minutes_to_close = 10
"""


def test_engine_discovers_and_scans_a_series_that_was_never_in_the_config(tmp_path):
    (tmp_path / "BRIEF.md").write_text("spec")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "bot.toml").write_text(TOML)
    (tmp_path / "k.pem").write_bytes(KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
    (tmp_path / ".env").write_text("KALSHI_API_KEY_ID=kid\nKALSHI_PRIVATE_KEY_PATH=./k.pem\n")

    close = (NOW + timedelta(minutes=12)).isoformat().replace("+00:00", "Z")
    def raw(series, ticker, category="Crypto"):
        return {"event_ticker": ticker, "series_ticker": series, "category": category, "mutually_exclusive": False,
                "title": f"{series} market", "markets": [
                    {"ticker": f"{ticker}-T90000", "event_ticker": ticker, "status": "active", "close_time": close,
                     "category": category, "strike_type": "greater", "floor_strike": "90000",
                     "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.42"}]}

    events = [raw("KXBTC", "KXBTC-H15"), raw("KXNBA", "KXNBA-1", category="Sports")]
    books = {"KXBTC-H15-T90000": {"yes_dollars": [["0.40", "50.00"]], "no_dollars": [["0.58", "50.00"]]},
             "KXNBA-1-T90000": {"yes_dollars": [["0.40", "50.00"]], "no_dollars": [["0.58", "50.00"]]}}
    series_meta = {t: {"ticker": t, "title": t, "category": "Crypto", "fee_type": "quadratic", "fee_multiplier": "0.07"}
                   for t in ("KXBTCD", "KXBTC")}
    ex = FakeKalshi(events, books, series_meta)

    def router(req):
        if req.url.host in ("api.kraken.com", "www.deribit.com"):
            return kraken_handler(req) if req.url.host == "api.kraken.com" else httpx.Response(503, json={})
        return ex.handler(req)

    settings = load_settings(tmp_path, environ={})
    feed = CryptoFeed(vol_source="realized", transport=httpx.MockTransport(router))
    eng = Engine(settings, trade=False, transport=httpx.MockTransport(router), feeds={"crypto": feed}, use_ws=False)

    async def go():
        with mock.patch("kalshi_bot.engine.datetime") as dt:
            dt.now.return_value = NOW
            await eng.startup()
            await eng.maybe_discover()
            strat = eng.strategies[0]
            assert "KXBTC" in strat.series()          # adopted from discovery
            assert "KXNBA" not in strat.series()      # blocked category never reaches a strategy
            rep = await eng.scan_once()
        await eng.close()
        return rep

    rep = asyncio.run(go())
    assert rep.markets >= 1 and ex.orders == []
    st = Storage(settings.db_path)
    priced = [d for d in st.decisions(strategy="crypto") if d["model_prob"]]
    assert any(d["market_ticker"] == "KXBTC-H15-T90000" for d in priced)
    st.close()
