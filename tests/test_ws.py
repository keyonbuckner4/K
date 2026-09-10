import asyncio
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_bot import auth
from kalshi_bot.errors import UnexpectedApiResponse
from kalshi_bot.ws import BookFeed

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def feed(**kw):
    return BookFeed("wss://demo.test/trade-api/ws/v2", auth.KalshiSigner("kid", KEY), **kw)


def run(coro):
    return asyncio.run(coro)


def test_handshake_headers_sign_ws_path():
    f = feed()
    h = f.auth_headers()
    ts = int(h["KALSHI-ACCESS-TIMESTAMP"])
    assert auth.verify(KEY.public_key(), h["KALSHI-ACCESS-SIGNATURE"], ts, "GET", "/trade-api/ws/v2")


def test_subscribe_command_shape():
    f = feed()
    cmd = f.subscribe_cmd("orderbook_delta", ["A", "B"])
    assert cmd == {"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_tickers": ["A", "B"]}}
    assert f.subscribe_cmd("fill") == {"id": 2, "cmd": "subscribe", "params": {"channels": ["fill"]}}
    assert f.update_cmd(7, "add_markets", ["C"])["params"] == {"sids": [7], "action": "add_markets", "market_tickers": ["C"]}
    assert f.unsubscribe_cmd(7)["params"] == {"sids": [7]}


def test_snapshot_then_deltas_rebuild_book():
    f = feed()

    async def go():
        assert await f.process_frame({"id": 1, "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 3}}) == "subscribed"
        assert await f.process_frame({"type": "orderbook_snapshot", "sid": 3, "seq": 1, "msg": {
            "market_ticker": "A", "market_id": "m", "yes_dollars_fp": [["0.40", "10.00"]], "no_dollars_fp": [["0.55", "5.00"]]}}) == "snapshot"
        b = f.book("A")
        assert b.best_yes_ask == Decimal("0.45") and b.yes_ask_size == Decimal("5.00") and b.seq == 1
        assert await f.process_frame({"type": "orderbook_delta", "sid": 3, "seq": 2, "msg": {
            "market_ticker": "A", "market_id": "m", "price_dollars": "0.55", "delta_fp": "-5.00", "side": "no"}}) == "delta"
        assert b.best_no_bid is None and b.best_yes_ask is None
        # polymorphic numbers and legacy cents are both accepted
        assert await f.process_frame({"type": "orderbook_delta", "sid": 3, "seq": 3, "msg": {
            "market_ticker": "A", "market_id": "m", "price": 60, "delta": 2.5, "side": "no"}}) == "delta"
        assert b.best_no_bid == Decimal("0.6") and b.no_bids[Decimal("0.6")] == Decimal("2.5")
        assert not b.stale and f.state.gaps == 0

    run(go())


def test_sequence_gap_marks_stale_and_requests_resync():
    f = feed()

    async def go():
        await f.process_frame({"type": "orderbook_snapshot", "sid": 3, "seq": 10, "msg": {"market_ticker": "A", "yes": [[40, 10]], "no": [[55, 5]]}})
        assert f.book("A").best_yes_bid == Decimal("0.4")
        r = await f.process_frame({"type": "orderbook_delta", "sid": 3, "seq": 12, "msg": {"market_ticker": "A", "price_dollars": "0.40", "delta_fp": "1", "side": "yes"}})
        assert r == "gap" and f.book("A").stale and f.resync_needed.is_set() and f.state.gaps == 1
        assert f.fresh_book("A", 60) is None

    run(go())


def test_delta_before_snapshot_is_flagged():
    f = feed()

    async def go():
        r = await f.process_frame({"type": "orderbook_delta", "sid": 1, "seq": 1, "msg": {"market_ticker": "Z", "price_dollars": "0.40", "delta_fp": "1", "side": "yes"}})
        assert r == "delta_without_snapshot" and f.book("Z").stale

    run(go())


def test_fill_and_error_frames():
    got = []
    f = feed(on_fill=lambda fill: got.append(fill))

    async def go():
        r = await f.process_frame({"type": "fill", "sid": 9, "msg": {"trade_id": "t1", "order_id": "o1", "market_ticker": "A", "is_taker": True,
                                                                     "yes_price_dollars": "0.45", "count_fp": "5.00", "fee_cost": "0.02",
                                                                     "book_side": "bid", "outcome_side": "yes", "ts": 1757500000}})
        assert r == "fill" and got[0].ticker == "A" and got[0].count == Decimal("5.00")
        assert await f.process_frame({"id": 4, "type": "error", "msg": {"code": 6, "msg": "Already subscribed"}}) == "error"
        assert f.state.errors[0]["code"] == 6
        with pytest.raises(UnexpectedApiResponse):
            await f.process_frame({"type": "orderbook_delta", "sid": 1, "msg": {}})

    run(go())
