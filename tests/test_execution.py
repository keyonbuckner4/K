import asyncio
import json
from decimal import Decimal

import httpx

from kalshi_bot import auth
from kalshi_bot.client import KalshiClient
from kalshi_bot.execution import Executor
from kalshi_bot.intent import ARB, Intent, Leg
from kalshi_bot.models import RateBudget
from kalshi_bot.ratelimit import SharedRateLimiter
from kalshi_bot.risk import RiskEngine
from kalshi_bot.storage import Storage
from cryptography.hazmat.primitives.asymmetric import rsa

from helpers import NOW, market, settings, snapshot

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Exchange:
    """Tiny fake exchange: records orders, fills what `fills` says."""

    def __init__(self, fills=None):
        self.orders = []
        self.cancels = []
        self.fills = fills or {}

    def handler(self, req: httpx.Request):
        p = req.url.path
        if req.method == "POST" and p.endswith("/portfolio/events/orders"):
            body = json.loads(req.content)
            self.orders.append(body)
            count = Decimal(body["count"])
            filled = self.fills.get(body["ticker"], count)
            return httpx.Response(201, json={"order_id": f"o{len(self.orders)}", "client_order_id": body["client_order_id"],
                                             "fill_count": f"{min(filled, count):.2f}", "remaining_count": f"{max(count - filled, 0):.2f}",
                                             "average_fill_price": body["price"], "average_fee_paid": "0.02", "ts_ms": 1})
        if req.method == "DELETE" and "/portfolio/events/orders" in p:
            self.cancels.append(p)
            return httpx.Response(200, json={"order_id": "x", "reduced_by": "1.00", "ts_ms": 1})
        if p.endswith("/orderbook"):
            return httpx.Response(200, json={"orderbook_fp": {"yes_dollars": [["0.40", "100"]], "no_dollars": [["0.55", "100"]]}})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": p}})


def build(tmp_path, exchange, mode):
    s = settings(tmp_path)
    lim = SharedRateLimiter(RateBudget(200, 400), RateBudget(100, 100), 10, 0.3)
    client = KalshiClient(s, lim, auth.KalshiSigner("kid", KEY), transport=httpx.MockTransport(exchange.handler))
    storage = Storage(tmp_path / "x.db")
    return Executor(client, storage, RiskEngine(storage, s, clock=lambda: NOW), mode=mode), storage


def directional():
    return Intent("weather", "KXHIGHNY-26SEP10", [Leg("KXHIGHNY-26SEP10-B70", "bid", Decimal("0.45"), 5, market=market(), model_prob=Decimal("0.6"))])


def basket():
    legs = [Leg("KXHIGHNY-26SEP10-B70", "bid", Decimal("0.45"), 5, market=market()),
            Leg("KXHIGHNY-26SEP10-B72", "bid", Decimal("0.40"), 5, market=market(ticker="KXHIGHNY-26SEP10-B72"))]
    return Intent("ladder_arb", "KXHIGHNY-26SEP10", legs, kind=ARB, expected_edge_cents=Decimal("8"))


def test_observe_mode_never_sends_an_order(tmp_path):
    ex = Exchange()
    executor, storage = build(tmp_path, ex, "observe")
    r = asyncio.run(executor.execute(directional(), snapshot()))
    assert r.status == "observed" and ex.orders == []
    assert storage.intents()[0]["status"] == "observed"
    assert any("OBSERVE" in d["reason"] for d in storage.decisions())


def test_trade_mode_places_v2_limit_orders_after_risk(tmp_path):
    ex = Exchange()
    executor, storage = build(tmp_path, ex, "trade")
    r = asyncio.run(executor.execute(directional(), snapshot()))
    assert r.status == "filled" and len(ex.orders) == 1
    o = ex.orders[0]
    assert o["side"] == "bid" and o["price"] == "0.4500" and o["count"] == "5.00" and o["time_in_force"] == "immediate_or_cancel"
    assert storage.orders()[0]["status"] == "executed" and storage.intents()[0]["status"] == "filled"


def test_risk_rejection_blocks_order(tmp_path):
    ex = Exchange()
    executor, storage = build(tmp_path, ex, "trade")
    r = asyncio.run(executor.execute(directional(), snapshot(balance_cents=100)))  # $1 equity -> 1% cap = 1c
    assert r.status == "rejected" and ex.orders == []
    assert storage.decisions()[0]["accepted"] == 0 and "risk rejected" in storage.decisions()[0]["reason"]


def test_partial_basket_is_unwound(tmp_path):
    ex = Exchange(fills={"KXHIGHNY-26SEP10-B72": Decimal("0")})  # second leg does not fill
    executor, storage = build(tmp_path, ex, "trade")
    r = asyncio.run(executor.execute(basket(), snapshot()))
    assert r.status == "unwound", r.reason
    # orders: leg1 (filled), leg2 (unfilled), unwind of leg1 as reduce-only ask
    assert len(ex.orders) == 3
    unwind = ex.orders[2]
    assert unwind["side"] == "ask" and unwind["reduce_only"] is True and unwind["ticker"] == "KXHIGHNY-26SEP10-B70"
    assert Decimal(unwind["price"]) == Decimal("0.37")  # best yes bid 0.40 minus 3c through


def test_flatten_all_cancels_and_closes(tmp_path):
    ex = Exchange()
    executor, storage = build(tmp_path, ex, "trade")
    from helpers import position
    res = asyncio.run(executor.flatten_all([position("KXHIGHNY-26SEP10-B70", 3), position("KXHIGHNY-26SEP10-B72", -2)], "daily halt"))
    assert len(ex.cancels) == 1 and ex.cancels[0].endswith("/portfolio/events/orders")
    sides = [(o["ticker"], o["side"], o["count"]) for o in ex.orders]
    assert sides == [("KXHIGHNY-26SEP10-B70", "ask", "3.00"), ("KXHIGHNY-26SEP10-B72", "bid", "2.00")]
    assert all(o["reduce_only"] for o in ex.orders) and len(res) == 2


def test_flatten_in_observe_mode_only_logs(tmp_path):
    ex = Exchange()
    executor, storage = build(tmp_path, ex, "observe")
    from helpers import position
    asyncio.run(executor.flatten_all([position("KXHIGHNY-26SEP10-B70", 3)], "daily halt"))
    assert ex.orders == [] and ex.cancels == []
