"""End-to-end: Engine against a fake Kalshi. Observe mode sends nothing; trade mode sends V2 limit
orders only after the gate and RiskEngine, and the loss limits keep working across the loop."""

import asyncio
from decimal import Decimal
from datetime import datetime, timezone
from unittest import mock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_bot.config import load_settings
from kalshi_bot.data.nws import NWSClient
from kalshi_bot.engine import Engine

from fake_exchange import FakeKalshi, ladder
from test_model_strategies import nws_handler

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TOML = """
[hosts.demo]
rest = "https://demo.test/trade-api/v2"
ws = "wss://demo.test/trade-api/ws/v2"
[storage]
db_path = "data/bot.db"
[engine]
scan_interval_sec = 1
market_cache_ttl_sec = 0
[gate]
min_net_edge_cents = 5
[strategies.ladder_arb]
enabled = true
mode = "%(arb_mode)s"
series = ["KXHIGHNY"]
max_contracts_per_leg = 5
[strategies.weather]
enabled = true
mode = "%(weather_mode)s"
max_contracts = 5
nws_user_agent = "test"
[[strategies.weather.cities]]
series_high = "KXHIGHNY"
station = "KNYC"
lat = 40.7789
lon = -73.9692
"""


def make_root(tmp_path, arb_mode="observe", weather_mode="observe"):
    (tmp_path / "BRIEF.md").write_text("spec")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "bot.toml").write_text(TOML % {"arb_mode": arb_mode, "weather_mode": weather_mode})
    pem = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    (tmp_path / "k.pem").write_bytes(pem)
    (tmp_path / ".env").write_text("KALSHI_API_KEY_ID=kid\nKALSHI_PRIVATE_KEY_PATH=./k.pem\n")
    return tmp_path


def fixed_now():
    return datetime(2026, 9, 10, 13, 0, tzinfo=timezone.utc)   # 09:00 New York: inside the weather model's morning trading window


def build(tmp_path, exchange, trade=False, **modes):
    root = make_root(tmp_path, **modes)
    settings = load_settings(root, environ={})
    nws = NWSClient("test", transport=httpx.MockTransport(nws_handler))
    eng = Engine(settings, trade=trade, transport=httpx.MockTransport(exchange.handler), feeds={"nws": nws}, use_ws=False)
    return eng


SERIES = {"KXHIGHNY": {"ticker": "KXHIGHNY", "title": "NYC high", "category": "Climate and Weather", "fee_type": "quadratic", "fee_multiplier": "0.07"}}


def test_observe_mode_scans_and_logs_but_never_orders(tmp_path):
    ev, books = ladder([("0.10", "0.12"), ("0.18", "0.20"), ("0.28", "0.30"), ("0.15", "0.18")])  # asks sum 0.80
    ex = FakeKalshi([ev], books, SERIES)
    eng = build(tmp_path, ex)

    async def go():
        with mock.patch("kalshi_bot.engine.datetime") as dt:
            dt.now.return_value = fixed_now()
            info = await eng.startup()
            assert info["limiter"]["source"] == "api:basic" and info["balance_cents"] == 100_000
            rep = await eng.run(once=True)
        await eng.close()
        return rep

    rep = asyncio.run(go())
    assert rep.strategies == 2 and rep.events == 2 and rep.markets == 8 and rep.intents >= 1
    assert ex.orders == []
    statuses = {e["status"] for e in rep.executed}
    assert statuses <= {"observed", "rejected"} and "observed" in statuses
    assert not any(m == "POST" for m, _, _ in ex.requests)


def test_trade_mode_places_orders_and_respects_per_event_limit(tmp_path):
    ev, books = ladder([("0.10", "0.12"), ("0.18", "0.20"), ("0.28", "0.30"), ("0.15", "0.18")])
    ex = FakeKalshi([ev], books, SERIES)
    eng = build(tmp_path, ex, trade=True, arb_mode="trade", weather_mode="trade")

    async def go():
        with mock.patch("kalshi_bot.engine.datetime") as dt:
            dt.now.return_value = fixed_now()
            await eng.startup()
            rep = await eng.run(once=True)
        await eng.close()
        return rep

    rep = asyncio.run(go())
    basket = [o for o in ex.orders if o["client_order_id"].endswith(("-0", "-1", "-2", "-3")) and o["side"] == "bid"]
    assert len(ex.orders) >= 4
    for o in ex.orders:
        assert o["time_in_force"] == "immediate_or_cancel" and o["self_trade_prevention_type"] == "taker_at_cross"
        assert Decimal("0.01") <= Decimal(o["price"]) <= Decimal("0.99")
    statuses = [e["status"] for e in rep.executed]
    assert statuses.count("filled") >= 1
    # the arb basket plus at most one directional intent share the event; the rest are risk-rejected (max 2 per event)
    assert "rejected" in statuses
    from kalshi_bot.storage import Storage
    rejected = [d for d in Storage(eng.settings.db_path).decisions() if d["stage"] == "risk" and not d["accepted"]]
    assert any("already in event" in d["reason"] for d in rejected)


def test_loss_halt_flattens_in_trade_mode(tmp_path):
    ev, books = ladder([("0.10", "0.12"), ("0.18", "0.20"), ("0.28", "0.30"), ("0.15", "0.18")])
    ex = FakeKalshi([ev], books, SERIES)
    eng = build(tmp_path, ex, trade=True, arb_mode="trade")

    async def go():
        with mock.patch("kalshi_bot.engine.datetime") as dt:
            dt.now.return_value = fixed_now()
            await eng.startup()
            await eng.run(once=True)          # opens the basket
            assert ex.positions
            # account now shows a 4% realized loss for the day -> daily halt -> flatten
            eng.storage.set_state("day_baseline_realized_cents", 4000)
            rep = await eng.run(once=True)
        await eng.close()
        return rep

    rep = asyncio.run(go())
    assert rep.health["ok"] is False and "daily halt" in rep.health["reason"]
    assert ex.cancels and ex.positions == {}  # everything closed with reduce-only orders
    assert all(o.get("reduce_only") for o in ex.orders[-4:])
    assert all(e["status"] == "halted" for e in rep.executed)


def test_unknown_series_is_skipped_not_guessed(tmp_path):
    ev, books = ladder([("0.10", "0.12"), ("0.18", "0.20"), ("0.28", "0.30"), ("0.15", "0.18")])
    ex = FakeKalshi([ev], books, series={})  # no fee parameters available
    eng = build(tmp_path, ex)

    async def go():
        with mock.patch("kalshi_bot.engine.datetime") as dt:
            dt.now.return_value = fixed_now()
            await eng.startup()
            rep = await eng.run(once=True)
        await eng.close()
        return rep

    rep = asyncio.run(go())
    assert rep.intents == 0 and rep.markets == 0


def test_doctor_probes_hosts(tmp_path):
    ev, books = ladder([("0.10", "0.12"), ("0.18", "0.20"), ("0.28", "0.30"), ("0.15", "0.18")])
    ex = FakeKalshi([ev], books, SERIES)
    eng = build(tmp_path, ex)

    async def go():
        out = await eng.doctor()
        await eng.close()
        return out

    out = asyncio.run(go())
    assert out["configured"]["rest"] == "https://demo.test/trade-api/v2"
    assert out["probes"][0]["status_code"] == 200 and out["auth"]["ok"] and out["limits"]["tier"] == "basic"
    assert len(out["probes"]) == 3  # configured + the two known demo host families


def test_observe_reads_production_data_while_account_stays_on_demo(tmp_path):
    """Default market_data = auto: events/books come from the live host, balance/positions from demo."""
    ev, books = ladder([("0.10", "0.12"), ("0.18", "0.20"), ("0.28", "0.30"), ("0.15", "0.18")])
    demo = FakeKalshi([], {}, SERIES)                 # demo carries no markets
    live = FakeKalshi([ev], books, SERIES)            # production does
    hosts = {}

    def router(req: httpx.Request):
        hosts.setdefault(req.url.host, set()).add(req.url.path)
        return (live if req.url.host == "live.test" else demo).handler(req)

    root = make_root(tmp_path)
    toml = (root / "config" / "bot.toml").read_text() + "\n[hosts.live]\nrest = \"https://live.test/trade-api/v2\"\nws = \"wss://live.test/trade-api/ws/v2\"\n"
    (root / "config" / "bot.toml").write_text(toml)
    settings = load_settings(root, environ={})
    nws = NWSClient("test", transport=httpx.MockTransport(nws_handler))
    eng = Engine(settings, trade=False, transport=httpx.MockTransport(router), feeds={"nws": nws}, use_ws=True)
    assert eng.data_env == "live" and eng.feed is None  # no live key -> REST polling for books

    async def go():
        with mock.patch("kalshi_bot.engine.datetime") as dt:
            dt.now.return_value = fixed_now()
            info = await eng.startup()
            rep = await eng.run(once=True)
        await eng.close()
        return info, rep

    info, rep = asyncio.run(go())
    assert info["market_data_env"] == "live" and info["env"] == "demo"
    assert rep.markets == 8 and rep.intents >= 1 and demo.orders == [] and live.orders == []
    assert "/trade-api/v2/events" in hosts["live.test"] and "/trade-api/v2/portfolio/balance" in hosts["demo.test"]
    assert not any("portfolio" in p for p in hosts["live.test"])  # never touches the live account


def test_trade_mode_uses_the_trading_venue_for_data(tmp_path):
    from kalshi_bot.errors import ConfigError

    ev, books = ladder([("0.10", "0.12"), ("0.18", "0.20"), ("0.28", "0.30"), ("0.15", "0.18")])
    ex = FakeKalshi([ev], books, SERIES)
    eng = build(tmp_path, ex, trade=True, arb_mode="trade")
    assert eng.data_env == "demo" and eng.data_client is eng.client
    asyncio.run(eng.close())
    root = tmp_path
    toml = (root / "config" / "bot.toml").read_text() + "\n[hosts.live]\nrest = \"https://live.test/trade-api/v2\"\nws = \"wss://live.test/trade-api/ws/v2\"\n"
    (root / "config" / "bot.toml").write_text(toml.replace("[engine]", "[engine]\nmarket_data = \"live\""))
    settings = load_settings(root, environ={})
    with pytest.raises(ConfigError, match="cannot be combined with --trade"):
        Engine(settings, trade=True, transport=httpx.MockTransport(ex.handler), use_ws=False)


def test_engine_scores_models_against_settlements(tmp_path):
    from decimal import Decimal

    ev, books = ladder([("0.10", "0.12"), ("0.18", "0.20"), ("0.28", "0.30"), ("0.15", "0.18")])
    settled = {"ticker": "KXHIGHNY-26SEP09-B76", "event_ticker": "KXHIGHNY-26SEP09", "status": "settled", "result": "yes",
               "close_time": "2026-09-09T23:00:00Z", "expiration_time": "2026-09-10T12:00:00Z", "strike_type": "between", "floor_strike": "76", "cap_strike": "77"}
    old_event = {"event_ticker": "KXHIGHNY-26SEP09", "series_ticker": "KXHIGHNY", "mutually_exclusive": True, "markets": [settled]}
    ex = FakeKalshi([ev, old_event], books, SERIES)
    eng = build(tmp_path, ex)
    # a model view logged earlier for the now-settled market
    eng.storage.log_decision("weather", "model", True, "candidate", market_ticker="KXHIGHNY-26SEP09-B76", model_prob=Decimal("0.7"),
                             price=Decimal("0.40"), book_side="bid", count=5)

    async def go():
        card = await eng.score_models()
        await eng.close()
        return card

    card = asyncio.run(go())
    assert card["synced_results"] == 1 and card["n_scored"] == 1 and card["n_candidates"] == 1
    assert card["brier_model_paired"] < card["brier_market"]  # 0.7 vs market 0.40 on a YES settlement
    assert card["verdict"].startswith("insufficient evidence")
    from kalshi_bot.storage import Storage
    st = Storage(eng.settings.db_path)
    assert st.get_state("last_backtest")["n_scored"] == 1 and st.market_results(["KXHIGHNY-26SEP09-B76"]) == {"KXHIGHNY-26SEP09-B76": "yes"}


def test_run_serves_the_dashboard_before_the_exchange_handshake(tmp_path, monkeypatch):
    """`bot run --dashboard`: the status page answers while startup is still in progress (so a slow or
    failing exchange never looks like a dead dashboard) and reports the bot's phase and last scan."""
    import argparse
    import http.client
    import json

    from kalshi_bot import cli, dashboard as dashboard_mod

    ev, books = ladder([("0.10", "0.12"), ("0.18", "0.20"), ("0.28", "0.30"), ("0.15", "0.18")])
    ex = FakeKalshi([ev], books, SERIES)
    eng = build(tmp_path, ex)
    monkeypatch.setattr(cli, "_engine", lambda *a, **k: eng)
    created = []

    class Recording(dashboard_mod.Dashboard):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            created.append(self)

    monkeypatch.setattr(dashboard_mod, "Dashboard", Recording)
    gate = asyncio.Event()
    real_startup = eng.startup

    async def slow_startup():
        await gate.wait()
        return await real_startup()

    eng.startup = slow_startup
    args = argparse.Namespace(trade=False, no_ws=True, interval=None, dashboard=True, port=0, strategy=None)
    seen = []

    async def probe():
        loop = asyncio.get_running_loop()
        while not created:
            await asyncio.sleep(0.01)
        host, port = created[0].server.server_address[:2]

        def get():
            c = http.client.HTTPConnection(host, port, timeout=5)
            c.request("GET", "/api/status")
            return json.loads(c.getresponse().read())

        first = await loop.run_in_executor(None, get)
        seen.append(first["bot"])
        gate.set()  # let startup proceed only after the page answered
        for _ in range(400):
            if eng.last_report:
                break
            await asyncio.sleep(0.025)
        seen.append((await loop.run_in_executor(None, get))["bot"])
        eng.stop.set()

    async def go():
        with mock.patch("kalshi_bot.engine.datetime") as dt:
            dt.now.return_value = fixed_now()
            await asyncio.gather(cli.cmd_run(args, eng.settings), probe())

    asyncio.run(go())
    assert seen[0]["phase"].startswith("starting") and seen[0]["last_scan_ts"] is None and seen[0]["pid"]
    assert seen[1]["phase"] == "running" and seen[1]["last_scan_ts"] and seen[1]["modes"] == {"ladder_arb": "observe", "weather": "observe"}
    assert seen[1]["market_data_env"] == "demo" and seen[1]["orders_env"] == "demo"
    assert ex.orders == []


def test_run_logs_a_failed_startup(tmp_path, monkeypatch, caplog):
    import argparse

    from kalshi_bot import cli
    from kalshi_bot.errors import ApiError

    ev, books = ladder([("0.10", "0.12"), ("0.18", "0.20"), ("0.28", "0.30"), ("0.15", "0.18")])
    eng = build(tmp_path, FakeKalshi([ev], books, SERIES))
    monkeypatch.setattr(cli, "_engine", lambda *a, **k: eng)

    async def broken_startup():
        raise ApiError(503, "GET", "/exchange/status", {"message": "exchange unreachable"})

    eng.startup = broken_startup
    args = argparse.Namespace(trade=False, no_ws=True, interval=None, dashboard=False, port=None, strategy=None)
    with pytest.raises(ApiError):
        asyncio.run(cli.cmd_run(args, eng.settings))
    assert "startup failed: ApiError:" in caplog.text and "exchange unreachable" in caplog.text


def test_watchdog_stops_a_hung_run(tmp_path):
    """A scan that never returns must not leave a process that looks alive: the watchdog cancels the run
    and it exits with a BotError so the supervisor restarts the bot."""
    from kalshi_bot.errors import BotError

    ev, books = ladder([("0.10", "0.12"), ("0.18", "0.20"), ("0.28", "0.30"), ("0.15", "0.18")])
    eng = build(tmp_path, FakeKalshi([ev], books, SERIES))
    eng.watchdog_sec = 1.0
    eng.scan_interval = 0.1
    real_scan = eng.scan_once
    calls = []

    async def scan_then_hang():
        calls.append(1)
        if len(calls) == 1:
            return await real_scan()
        await asyncio.Event().wait()   # hangs forever

    eng.scan_once = scan_then_hang

    async def go():
        with mock.patch("kalshi_bot.engine.datetime") as dt:
            dt.now.return_value = fixed_now()
            await eng.startup()
            try:
                await eng.run(once=False)
            finally:
                await eng.close()

    with pytest.raises(BotError, match="watchdog"):
        asyncio.run(go())
