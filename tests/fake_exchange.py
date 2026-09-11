"""A small in-memory Kalshi for end-to-end tests. Shapes follow the documented V2 API."""

import json
from decimal import Decimal

import httpx


class FakeKalshi:
    def __init__(self, events, books, series=None, balance_cents=100_000, fills=None):
        self.events = events            # list of event dicts with nested markets
        self.books = books              # ticker -> {"yes_dollars": [[p, q]], "no_dollars": [[p, q]]}
        self.series = series or {}
        self.balance_cents = balance_cents
        self.positions = {}             # ticker -> Decimal signed
        self.orders = []
        self.cancels = []
        self.fill_rule = fills or {}    # ticker -> fill count (default: full)
        self.requests = []

    def _market(self, ticker):
        for e in self.events:
            for m in e["markets"]:
                if m["ticker"] == ticker:
                    return m
        return None

    def handler(self, req: httpx.Request):
        self.requests.append((req.method, req.url.path, dict(req.url.params)))
        p = req.url.path
        q = req.url.params
        if p.endswith("/exchange/status"):
            return httpx.Response(200, json={"exchange_active": True, "trading_active": True})
        if p.endswith("/portfolio/balance"):
            return httpx.Response(200, json={"balance": self.balance_cents, "balance_dollars": f"{self.balance_cents / 100:.2f}", "portfolio_value": 0, "updated_ts": 1})
        if p.endswith("/account/limits"):
            return httpx.Response(200, json={"usage_tier": "basic", "read": {"refill_rate": 200, "bucket_capacity": 400}, "write": {"refill_rate": 100, "bucket_capacity": 100}, "grants": []})
        if p.endswith("/portfolio/positions"):
            rows = [{"ticker": t, "event_ticker": self._market(t)["event_ticker"] if self._market(t) else None, "position_fp": f"{v:.2f}",
                     "market_exposure_dollars": "0", "realized_pnl_dollars": "0", "fees_paid_dollars": "0", "resting_orders_count": 0,
                     "last_updated_ts": "2026-09-10T15:00:00Z"} for t, v in self.positions.items()]
            return httpx.Response(200, json={"market_positions": rows, "event_positions": [], "cursor": None})
        if p.endswith("/portfolio/orders") and req.method == "GET":
            return httpx.Response(200, json={"orders": [], "cursor": None})
        if p.endswith("/portfolio/fills"):
            return httpx.Response(200, json={"fills": [], "cursor": None})
        if "/series/" in p and "/markets/" not in p:
            t = p.rsplit("/", 1)[1]
            if t in self.series:
                return httpx.Response(200, json={"series": self.series[t]})
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "series"}})
        if p.endswith("/events"):
            evs = [e for e in self.events if not q.get("series_ticker") or e["series_ticker"] == q.get("series_ticker")]
            return httpx.Response(200, json={"events": evs, "cursor": None})
        if p.endswith("/markets/orderbooks"):
            tickers = [t for v in q.get_list("tickers") for t in v.split(",") if t]  # exploded form, comma form tolerated
            return httpx.Response(200, json={"orderbooks": [{"ticker": t, "orderbook_fp": self.books[t]} for t in tickers if t in self.books]})
        if p.endswith("/orderbook"):
            t = p.split("/")[-2]
            return httpx.Response(200, json={"orderbook_fp": self.books.get(t, {"yes_dollars": [], "no_dollars": []})})
        if p.endswith("/markets") and req.method == "GET":
            tickers = q.get("tickers", "").split(",") if q.get("tickers") else None
            ms = [m for e in self.events for m in e["markets"] if not tickers or m["ticker"] in tickers]
            return httpx.Response(200, json={"markets": ms, "cursor": None})
        if p.endswith("/portfolio/events/orders") and req.method == "POST":
            body = json.loads(req.content)
            self.orders.append(body)
            count = Decimal(body["count"])
            filled = min(count, Decimal(self.fill_rule.get(body["ticker"], count)))
            signed = filled if body["side"] == "bid" else -filled
            self.positions[body["ticker"]] = self.positions.get(body["ticker"], Decimal(0)) + signed
            if self.positions[body["ticker"]] == 0:
                del self.positions[body["ticker"]]
            return httpx.Response(201, json={"order_id": f"o{len(self.orders)}", "client_order_id": body["client_order_id"], "fill_count": f"{filled:.2f}",
                                             "remaining_count": f"{count - filled:.2f}", "average_fill_price": body["price"], "average_fee_paid": "0.02", "ts_ms": 1})
        if "/portfolio/events/orders" in p and req.method == "DELETE":
            self.cancels.append(p)
            return httpx.Response(200, json={"order_id": "x", "reduced_by": "0.00", "ts_ms": 1})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": p}})


def ladder(prices, event="KXHIGHNY-26SEP10", series="KXHIGHNY", close="2026-09-10T23:00:00Z", category="Climate and Weather"):
    """prices: (yes_bid, yes_ask) for <=75, 76-77, 78-79, >=80."""
    specs = [("T75", "less_or_equal", None, "75"), ("B76", "between", "76", "77"), ("B78", "between", "78", "79"), ("T80", "greater_or_equal", "80", None)]
    markets, books = [], {}
    for (suffix, st, fl, cp), (bid, ask) in zip(specs, prices):
        t = f"{event}-{suffix}"
        markets.append({"ticker": t, "event_ticker": event, "status": "open", "close_time": close, "category": category, "strike_type": st,
                        "floor_strike": fl, "cap_strike": cp, "yes_bid_dollars": bid, "yes_ask_dollars": ask, "rules_primary": "NWS Central Park daily climate report"})
        books[t] = {"yes_dollars": [[bid, "50.00"]], "no_dollars": [[f"{1 - float(ask):.4f}", "50.00"]]}
    ev = {"event_ticker": event, "series_ticker": series, "mutually_exclusive": True, "category": category, "markets": markets, "title": "High temp NYC"}
    return ev, books
