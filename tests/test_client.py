"""REST client tests with an httpx MockTransport. No network, no real credentials."""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_bot import auth
from kalshi_bot.client import KalshiClient
from kalshi_bot.config import Settings
from kalshi_bot.errors import ApiError, ConfigError, RateLimited, UnexpectedApiResponse
from kalshi_bot.models import RateBudget
from kalshi_bot.ratelimit import SharedRateLimiter

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def settings(tmp_path):
    return Settings(env="demo", root=tmp_path, rest_base_url="https://demo.test/trade-api/v2", ws_url="wss://demo.test/trade-api/ws/v2",
                    api_key_id="kid", private_key_path=None, alert_webhook_url=None, db_path=tmp_path / "x.db",
                    halt_path=tmp_path / "HALT", toml={})


def make_client(tmp_path, handler, signer=True, **kw):
    lim = SharedRateLimiter(RateBudget(200, 400), RateBudget(100, 100), 10, 0.3)
    s = auth.KalshiSigner("kid", KEY) if signer else None

    async def nosleep(_):
        pass

    return KalshiClient(settings(tmp_path), lim, s, transport=httpx.MockTransport(handler), sleep=nosleep, **kw)


def run(coro):
    return asyncio.run(coro)


def test_balance_signs_path_without_query_and_parses(tmp_path):
    seen = {}

    def handler(req: httpx.Request):
        seen["headers"] = dict(req.headers)
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"balance": 12345, "balance_dollars": "123.45", "portfolio_value": 100, "updated_ts": 1757500000})

    c = make_client(tmp_path, handler)
    b = run(c.balance())
    assert b.balance_cents == 12345 and b.equity_cents == 12445
    h = seen["headers"]
    assert h["kalshi-access-key"] == "kid"
    ts = int(h["kalshi-access-timestamp"])
    assert auth.verify(KEY.public_key(), h["kalshi-access-signature"], ts, "GET", "/trade-api/v2/portfolio/balance")


def test_query_string_sent_but_not_signed(tmp_path):
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["headers"] = dict(req.headers)
        return httpx.Response(200, json={"markets": [{"ticker": "A-1-X", "event_ticker": "A-1", "status": "open"}], "cursor": ""})

    c = make_client(tmp_path, handler)
    ms = run(c.markets(series_ticker="KXHIGHNY", status="open", limit=5))
    assert seen["url"] == "https://demo.test/trade-api/v2/markets?series_ticker=KXHIGHNY&status=open&limit=5"
    ts = int(seen["headers"]["kalshi-access-timestamp"])
    assert auth.verify(KEY.public_key(), seen["headers"]["kalshi-access-signature"], ts, "GET", "/trade-api/v2/markets")
    assert ms[0].ticker == "A-1-X"


def test_pagination_follows_cursor(tmp_path):
    calls = []

    def handler(req: httpx.Request):
        calls.append(dict(req.url.params))
        cursor = req.url.params.get("cursor")
        if not cursor:
            return httpx.Response(200, json={"market_positions": [{"ticker": "A-1-X", "position_fp": "2.00"}], "cursor": "c2"})
        return httpx.Response(200, json={"market_positions": [{"ticker": "A-1-Y", "position_fp": "-1.00"}], "cursor": None})

    c = make_client(tmp_path, handler)
    ps = run(c.positions())
    assert [p.ticker for p in ps] == ["A-1-X", "A-1-Y"]
    assert calls[1]["cursor"] == "c2"


def test_create_order_v2_body_shape(tmp_path):
    seen = {}

    def handler(req: httpx.Request):
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        seen["headers"] = dict(req.headers)
        return httpx.Response(201, json={"order_id": "o1", "client_order_id": seen["body"]["client_order_id"], "fill_count": "5.00",
                                         "remaining_count": "0.00", "average_fill_price": "0.4500", "average_fee_paid": "0.02", "ts_ms": 1})

    c = make_client(tmp_path, handler)
    ack = run(c.create_order(ticker="KXHIGHNY-26SEP10-B70", book_side="bid", price=Decimal("0.45"), count=5, client_order_id="coid-1"))
    assert seen["path"] == "/trade-api/v2/portfolio/events/orders"
    assert seen["body"] == {"ticker": "KXHIGHNY-26SEP10-B70", "client_order_id": "coid-1", "side": "bid", "count": "5.00", "price": "0.4500",
                            "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"}
    ts = int(seen["headers"]["kalshi-access-timestamp"])
    assert auth.verify(KEY.public_key(), seen["headers"]["kalshi-access-signature"], ts, "POST", "/trade-api/v2/portfolio/events/orders")
    assert ack.order_id == "o1" and ack.fill_count == Decimal("5.00")


def test_order_body_validation():
    with pytest.raises(ValueError):
        KalshiClient.build_order_body(ticker="T", book_side="buy", price=Decimal("0.5"), count=1)
    with pytest.raises(ValueError):
        KalshiClient.build_order_body(ticker="T", book_side="bid", price=Decimal("1.0"), count=1)
    with pytest.raises(ValueError):
        KalshiClient.build_order_body(ticker="T", book_side="bid", price=Decimal("0.5"), count=0)
    with pytest.raises(ValueError):
        KalshiClient.build_order_body(ticker="T", book_side="bid", price=Decimal("0.5"), count=1, time_in_force="market")
    b = KalshiClient.build_order_body(ticker="T", book_side="ask", price=Decimal("0.5"), count=Decimal("2"), reduce_only=True, post_only=False)
    assert b["reduce_only"] is True and b["post_only"] is False and b["side"] == "ask"


def test_cancel_uses_v2_path(tmp_path):
    seen = {}

    def handler(req: httpx.Request):
        seen["method"], seen["path"] = req.method, req.url.path
        return httpx.Response(200, json={"order_id": "o1", "reduced_by": "5.00", "ts_ms": 1})

    c = make_client(tmp_path, handler)
    r = run(c.cancel_order("o1"))
    assert (seen["method"], seen["path"]) == ("DELETE", "/trade-api/v2/portfolio/events/orders/o1")
    assert r["reduced_by"] == "5.00"


def test_429_backs_off_then_succeeds_and_penalizes_limiter(tmp_path):
    n = {"calls": 0}

    def handler(req: httpx.Request):
        n["calls"] += 1
        if n["calls"] < 3:
            return httpx.Response(429, json={"error": {"code": "rate_limited", "message": "slow down"}})
        return httpx.Response(200, json={"exchange_active": True, "trading_active": True})

    c = make_client(tmp_path, handler)
    s = run(c.exchange_status())
    assert s.trading_active and n["calls"] == 3
    assert c.limiter.stats.throttled_429 == 2


def test_429_exhausted_raises_rate_limited(tmp_path):
    def handler(req: httpx.Request):
        return httpx.Response(429, json={"error": {"code": "rate_limited", "message": "slow down"}})

    c = make_client(tmp_path, handler, max_retries=1)
    with pytest.raises(RateLimited):
        run(c.exchange_status())


def test_post_is_not_retried_on_5xx_but_get_is(tmp_path):
    n = {"post": 0, "get": 0}

    def handler(req: httpx.Request):
        if req.method == "POST":
            n["post"] += 1
            return httpx.Response(502, json={"error": {"code": "bad_gateway", "message": "x"}})
        n["get"] += 1
        if n["get"] == 1:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"exchange_active": True, "trading_active": True})

    c = make_client(tmp_path, handler)
    with pytest.raises(ApiError) as ei:
        run(c.create_order(ticker="T", book_side="bid", price=Decimal("0.5"), count=1))
    assert ei.value.status == 502 and n["post"] == 1
    run(c.exchange_status())
    assert n["get"] == 2


def test_error_mapping_and_unexpected_shapes(tmp_path):
    def handler(req: httpx.Request):
        if req.url.path.endswith("/portfolio/balance"):
            return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "bad sig"}})
        if req.url.path.endswith("/series/KXX"):
            return httpx.Response(200, json={"nope": {}})
        return httpx.Response(200, json={"market": {"ticker": "A-1-X", "event_ticker": "A-1", "status": "open"}})

    c = make_client(tmp_path, handler)
    with pytest.raises(ApiError) as ei:
        run(c.balance())
    assert ei.value.status == 401 and ei.value.code == "unauthorized"
    with pytest.raises(UnexpectedApiResponse):
        run(c.series("KXX"))
    assert run(c.market("A-1-X")).ticker == "A-1-X"


def test_auth_required_without_signer(tmp_path):
    def handler(req: httpx.Request):
        return httpx.Response(200, json={"exchange_active": True, "trading_active": True})

    c = make_client(tmp_path, handler, signer=False)
    assert run(c.exchange_status()).exchange_active  # public endpoint works unsigned
    with pytest.raises(ConfigError):
        run(c.balance())


def test_orderbook_and_bulk_fallback(tmp_path):
    def handler(req: httpx.Request):
        if req.url.path.endswith("/markets/orderbooks"):
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "no"}})
        return httpx.Response(200, json={"orderbook_fp": {"yes_dollars": [["0.40", "10"]], "no_dollars": [["0.55", "5"]]}})

    c = make_client(tmp_path, handler)
    books = run(c.orderbooks(["A", "B"]))
    assert set(books) == {"A", "B"} and books["A"].best_yes_ask == Decimal("0.45")


def test_event_merges_top_level_markets(tmp_path):
    def handler(req: httpx.Request):
        return httpx.Response(200, json={"event": {"event_ticker": "KXHIGHNY-26SEP10", "series_ticker": "KXHIGHNY", "mutually_exclusive": True},
                                         "markets": [{"ticker": "KXHIGHNY-26SEP10-B70", "event_ticker": "KXHIGHNY-26SEP10", "status": "open"}]})

    c = make_client(tmp_path, handler)
    e = run(c.event("KXHIGHNY-26SEP10"))
    assert e.mutually_exclusive and len(e.markets) == 1 and e.series_ticker == "KXHIGHNY"


def test_public_list_endpoints_work_without_a_signer(tmp_path):
    seen = {}

    def handler(req: httpx.Request):
        seen["signed"] = "kalshi-access-signature" in req.headers
        return httpx.Response(200, json={"events": [{"event_ticker": "A-1", "series_ticker": "A", "markets": []}], "cursor": None})

    c = make_client(tmp_path, handler, signer=False)
    evs = run(c.events(series_ticker="A"))
    assert evs[0].event_ticker == "A-1" and seen["signed"] is False
    with pytest.raises(ConfigError):
        run(c.positions())  # private endpoints still need a key
