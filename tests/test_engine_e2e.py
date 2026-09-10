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
    return datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)


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
