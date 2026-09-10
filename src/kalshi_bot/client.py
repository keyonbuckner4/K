"""Async Kalshi REST client.

Every request goes through the one shared rate limiter. Reads are GETs; writes are everything
else. 429s drain our local bucket and back off with full jitter (Kalshi sends no Retry-After, but
one is honored if present). Only requests that are safe to repeat are retried on transport
errors: a POST that may have reached the exchange is never blindly resent.

Endpoint facts encoded here (Kalshi Trade API v2, 2026):
* Orders are created with ``POST /portfolio/events/orders`` (V2). The body is YES-referenced:
  ``side`` is ``bid`` (buy YES / sell NO) or ``ask`` (sell YES / buy NO), ``price`` is the YES
  price as a dollar string, ``count`` is a fixed-point string, ``time_in_force`` and
  ``self_trade_prevention_type`` are required. The legacy ``POST /portfolio/orders`` was removed
  in June 2026 and returns 410.
* Cancel: ``DELETE /portfolio/events/orders/{order_id}``. Cancel all: ``DELETE /portfolio/events/orders``.
* Reads keep their legacy paths (``/portfolio/orders``, ``/portfolio/fills``, ``/portfolio/positions``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from decimal import Decimal
from typing import Any, Mapping

import httpx

from . import __version__
from .auth import KalshiSigner
from .config import Settings
from .errors import ApiError, ConfigError, RateLimited, UnexpectedApiResponse
from .models import (AccountLimits, Balance, Event, ExchangeStatus, Fill, Market, Order, OrderAck, Position, Series,
                     Settlement, fmt_count, fmt_price)
from .orderbook import OrderBook
from .ratelimit import CRITICAL, NORMAL, READ, WRITE, SharedRateLimiter

log = logging.getLogger(__name__)

TIME_IN_FORCE = ("immediate_or_cancel", "good_till_canceled", "fill_or_kill")
STP_TYPES = ("taker_at_cross", "maker")
BOOK_SIDES = ("bid", "ask")
RETRYABLE_STATUS = {408, 425, 500, 502, 503, 504}
IDEMPOTENT = {"GET", "DELETE"}


class KalshiClient:
    def __init__(self, settings: Settings, limiter: SharedRateLimiter, signer: KalshiSigner | None = None, *,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0, max_retries: int = 4,
                 retry_base_delay: float = 0.5, retry_max_delay: float = 8.0, sleep=None):
        self.settings = settings
        self.limiter = limiter
        self.signer = signer
        self.base_url = settings.rest_base_url.rstrip("/")
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay
        self._sleep = sleep or asyncio.sleep
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout,
                                       headers={"User-Agent": f"kalshi-bot/{__version__}", "Accept": "application/json"})

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "KalshiClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ---- transport --------------------------------------------------------------------
    def _backoff(self, attempt: int, retry_after: float | None = None) -> float:
        capped = min(self.retry_max_delay, self.retry_base_delay * (2 ** attempt))
        delay = random.uniform(0, capped)
        if retry_after is not None:
            delay = min(self.retry_max_delay, retry_after) + delay
        return delay

    async def _request(self, method: str, path: str, *, params: Mapping[str, Any] | None = None, body: Any = None,
                       auth: bool = True, kind: str | None = None, priority: str = NORMAL, cost: int | None = None) -> Any:
        method = method.upper()
        kind = kind or (READ if method == "GET" else WRITE)
        url = f"{self.base_url}{path}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(self.max_retries + 1):
            await self.limiter.acquire(kind, cost, priority)
            headers: dict[str, str] = {}
            if auth:
                if self.signer is None:
                    raise ConfigError("this call needs credentials; set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env")
                headers.update(self.signer.headers(method, url))  # path only, query string excluded
            try:
                resp = await self._http.request(method, url, params=clean_params, json=body, headers=headers)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:  # nothing reached the server
                if attempt < self.max_retries:
                    d = self._backoff(attempt)
                    log.warning("%s %s connect failed (%s); retry in %.2fs", method, path, e, d)
                    await self._sleep(d)
                    continue
                raise ApiError(0, method, path, {"error": {"code": "connect", "message": str(e)}}) from e
            except httpx.HTTPError as e:  # request may have been delivered
                if method in IDEMPOTENT and attempt < self.max_retries:
                    d = self._backoff(attempt)
                    log.warning("%s %s transport error (%s); retry in %.2fs", method, path, e, d)
                    await self._sleep(d)
                    continue
                raise ApiError(0, method, path, {"error": {"code": "transport", "message": str(e)}}) from e

            status = resp.status_code
            try:
                payload: Any = resp.json() if resp.content else {}
            except json.JSONDecodeError:
                payload = {"raw_text": resp.text[:2000]}
            if 200 <= status < 300:
                return payload
            if status == 429:
                self.limiter.penalize(kind)
                ra = resp.headers.get("Retry-After")
                retry_after = float(ra) if ra and ra.replace(".", "", 1).isdigit() else None
                if attempt < self.max_retries:
                    d = self._backoff(attempt, retry_after)
                    log.warning("%s %s rate limited (429); retry in %.2fs", method, path, d)
                    await self._sleep(d)
                    continue
                raise RateLimited(status, method, path, payload)
            if status in RETRYABLE_STATUS and method in IDEMPOTENT and attempt < self.max_retries:
                d = self._backoff(attempt)
                log.warning("%s %s returned %d; retry in %.2fs", method, path, status, d)
                await self._sleep(d)
                continue
            raise ApiError(status, method, path, payload)
        raise ApiError(0, method, path, {"error": {"code": "retries_exhausted", "message": "unreachable"}})

    async def _paginate(self, path: str, key: str, params: dict[str, Any], max_pages: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            page = await self._request("GET", path, params={**params, "cursor": cursor})
            if not isinstance(page, Mapping) or key not in page:
                raise UnexpectedApiResponse(f"{path} response without '{key}'", page)
            items = page.get(key) or []
            if not isinstance(items, list):
                raise UnexpectedApiResponse(f"{path} '{key}' is not a list", page)
            out.extend(items)
            cursor = page.get("cursor") or None
            if not cursor:
                break
        return out

    # ---- public / exchange ----------------------------------------------------------------
    async def exchange_status(self) -> ExchangeStatus:
        return ExchangeStatus.parse(await self._request("GET", "/exchange/status", auth=self.signer is not None))

    async def series(self, ticker: str) -> Series:
        data = await self._request("GET", f"/series/{ticker}", auth=self.signer is not None)
        node = data.get("series") if isinstance(data, Mapping) else None
        if not isinstance(node, Mapping):
            raise UnexpectedApiResponse("GET /series/{ticker} without 'series'", data)
        return Series.parse(node)

    async def markets(self, *, series_ticker: str | None = None, event_ticker: str | None = None, status: str | None = "open",
                      tickers: list[str] | None = None, limit: int = 200, min_close_ts: int | None = None,
                      max_close_ts: int | None = None, max_pages: int = 20) -> list[Market]:
        params = {"series_ticker": series_ticker, "event_ticker": event_ticker, "status": status,
                  "tickers": ",".join(tickers) if tickers else None, "limit": limit,
                  "min_close_ts": min_close_ts, "max_close_ts": max_close_ts}
        return [Market.parse(m) for m in await self._paginate("/markets", "markets", params, max_pages)]

    async def market(self, ticker: str) -> Market:
        data = await self._request("GET", f"/markets/{ticker}", auth=self.signer is not None)
        node = data.get("market") if isinstance(data, Mapping) else None
        if not isinstance(node, Mapping):
            raise UnexpectedApiResponse("GET /markets/{ticker} without 'market'", data)
        return Market.parse(node)

    async def events(self, *, series_ticker: str | None = None, status: str | None = "open", with_nested_markets: bool = True,
                     limit: int = 100, max_pages: int = 5) -> list[Event]:
        params = {"series_ticker": series_ticker, "status": status, "with_nested_markets": str(with_nested_markets).lower(), "limit": limit}
        return [Event.parse(e) for e in await self._paginate("/events", "events", params, max_pages)]

    async def event(self, event_ticker: str, with_nested_markets: bool = True) -> Event:
        data = await self._request("GET", f"/events/{event_ticker}", params={"with_nested_markets": str(with_nested_markets).lower()},
                                   auth=self.signer is not None)
        node = data.get("event") if isinstance(data, Mapping) else None
        if not isinstance(node, Mapping):
            raise UnexpectedApiResponse("GET /events/{ticker} without 'event'", data)
        if not node.get("markets") and isinstance(data.get("markets"), list):
            node = {**node, "markets": data["markets"]}
        return Event.parse(node)

    async def orderbook(self, ticker: str, depth: int | None = None) -> OrderBook:
        data = await self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth}, auth=self.signer is not None)
        if not isinstance(data, Mapping):
            raise UnexpectedApiResponse("orderbook response is not an object", data)
        return OrderBook.from_payload(ticker, data)

    async def orderbooks(self, tickers: list[str]) -> dict[str, OrderBook]:
        """Bulk books via ``GET /markets/orderbooks`` (100 tickers max), per-ticker fallback on 404."""
        out: dict[str, OrderBook] = {}
        for i in range(0, len(tickers), 100):
            chunk = tickers[i:i + 100]
            try:
                data = await self._request("GET", "/markets/orderbooks", params={"tickers": ",".join(chunk)}, auth=self.signer is not None)
            except ApiError as e:
                if e.status != 404:
                    raise
                for t in chunk:
                    out[t] = await self.orderbook(t)
                continue
            items = data.get("orderbooks") if isinstance(data, Mapping) else None
            if not isinstance(items, list):
                raise UnexpectedApiResponse("GET /markets/orderbooks without 'orderbooks'", data)
            for item in items:
                t = item.get("ticker") if isinstance(item, Mapping) else None
                if not t:
                    raise UnexpectedApiResponse("bulk orderbook item without ticker", item)
                out[t] = OrderBook.from_payload(t, item)
        return out

    async def candlesticks(self, series_ticker: str, ticker: str, start_ts: int, end_ts: int, period_interval: int = 60) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/series/{series_ticker}/markets/{ticker}/candlesticks",
                                   params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
                                   auth=self.signer is not None)
        items = data.get("candlesticks") if isinstance(data, Mapping) else None
        if not isinstance(items, list):
            raise UnexpectedApiResponse("candlesticks response without 'candlesticks'", data)
        return items

    # ---- account --------------------------------------------------------------------------
    async def balance(self) -> Balance:
        return Balance.parse(await self._request("GET", "/portfolio/balance"))

    async def account_limits(self) -> AccountLimits:
        return AccountLimits.parse(await self._request("GET", "/account/limits"), self.limiter.default_cost)

    async def positions(self, *, settlement_status: str = "unsettled", ticker: str | None = None, event_ticker: str | None = None,
                        count_filter: str | None = "position", limit: int = 200) -> list[Position]:
        params = {"settlement_status": settlement_status, "ticker": ticker, "event_ticker": event_ticker,
                  "count_filter": count_filter, "limit": limit}
        rows = await self._paginate("/portfolio/positions", "market_positions", params)
        return [Position.parse(r) for r in rows]

    async def orders(self, *, status: str | None = "resting", ticker: str | None = None, event_ticker: str | None = None,
                     limit: int = 200) -> list[Order]:
        rows = await self._paginate("/portfolio/orders", "orders", {"status": status, "ticker": ticker, "event_ticker": event_ticker, "limit": limit})
        return [Order.parse(r) for r in rows]

    async def order(self, order_id: str) -> Order:
        data = await self._request("GET", f"/portfolio/orders/{order_id}")
        node = data.get("order") if isinstance(data, Mapping) and isinstance(data.get("order"), Mapping) else data
        return Order.parse(node)

    async def fills(self, *, ticker: str | None = None, order_id: str | None = None, min_ts: int | None = None, limit: int = 200,
                    max_pages: int = 10) -> list[Fill]:
        rows = await self._paginate("/portfolio/fills", "fills", {"ticker": ticker, "order_id": order_id, "min_ts": min_ts, "limit": limit}, max_pages)
        return [Fill.parse(r) for r in rows]

    async def settlements(self, *, limit: int = 200, max_pages: int = 10) -> list[Settlement]:
        rows = await self._paginate("/portfolio/settlements", "settlements", {"limit": limit}, max_pages)
        return [Settlement.parse(r) for r in rows]

    # ---- orders (V2) -----------------------------------------------------------------------
    @staticmethod
    def build_order_body(*, ticker: str, book_side: str, price: Decimal, count: int | Decimal, client_order_id: str | None = None,
                         time_in_force: str = "immediate_or_cancel", self_trade_prevention_type: str = "taker_at_cross",
                         post_only: bool | None = None, reduce_only: bool | None = None) -> dict[str, Any]:
        if book_side not in BOOK_SIDES:
            raise ValueError(f"book_side must be one of {BOOK_SIDES}, got {book_side!r}")
        if time_in_force not in TIME_IN_FORCE:
            raise ValueError(f"time_in_force must be one of {TIME_IN_FORCE}, got {time_in_force!r}")
        if self_trade_prevention_type not in STP_TYPES:
            raise ValueError(f"self_trade_prevention_type must be one of {STP_TYPES}")
        p = Decimal(price)
        if not (Decimal("0") < p < Decimal("1")):
            raise ValueError(f"price must be strictly between 0 and 1 dollars, got {p}")
        c = Decimal(count)
        if c <= 0:
            raise ValueError("count must be positive")
        body: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": book_side,
            "count": fmt_count(c),
            "price": fmt_price(p),
            "time_in_force": time_in_force,
            "self_trade_prevention_type": self_trade_prevention_type,
        }
        if post_only is not None:
            body["post_only"] = bool(post_only)
        if reduce_only is not None:
            body["reduce_only"] = bool(reduce_only)
        return body

    async def create_order(self, **kwargs: Any) -> OrderAck:
        """Limit orders only. There is deliberately no way to send a market order from this client."""
        body = self.build_order_body(**kwargs)
        log.info("ORDER %s %s %s @ %s tif=%s coid=%s", body["side"], body["count"], body["ticker"], body["price"],
                 body["time_in_force"], body["client_order_id"])
        data = await self._request("POST", "/portfolio/events/orders", body=body, kind=WRITE)
        return OrderAck.parse(data)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        data = await self._request("DELETE", f"/portfolio/events/orders/{order_id}", kind=WRITE, priority=CRITICAL)
        if not isinstance(data, Mapping) or "order_id" not in data:
            raise UnexpectedApiResponse("cancel response without order_id", data)
        return dict(data)

    async def cancel_all_orders(self) -> Any:
        return await self._request("DELETE", "/portfolio/events/orders", kind=WRITE, priority=CRITICAL)
