"""WebSocket market data: order book reconstruction from snapshots and deltas, plus fills.

Protocol (Kalshi Trade API WS v2):
* Connect to ``<ws_url>`` with the same three RSA-PSS headers as REST, signed over
  ``GET /trade-api/ws/v2`` (the URL path, no query string).
* ``{"id": n, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_tickers": [...]}}``
  is acknowledged with ``{"id": n, "type": "subscribed", "msg": {"channel": ..., "sid": S}}``.
* Frames on a subscription carry ``sid`` and, for order book channels, a monotonic ``seq``.
  A gap means frames were lost: the book is marked stale and the subscription is rebuilt.
* ``orderbook_snapshot`` carries full YES/NO bid ladders (``yes_dollars_fp``/``no_dollars_fp``,
  or legacy ``yes``/``no`` in cents); ``orderbook_delta`` carries ``side``, ``price_dollars``
  and a signed ``delta_fp``. Values may be strings or numbers.
* ``fill`` (private) reports our executions. ``market_lifecycle_v2`` reports settlements.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

from .auth import KalshiSigner
from .errors import UnexpectedApiResponse
from .models import Fill, dec
from .orderbook import OrderBook, parse_book_payload

log = logging.getLogger(__name__)

FillHandler = Callable[[Fill], Awaitable[None] | None]
LifecycleHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass
class FeedState:
    books: dict[str, OrderBook] = field(default_factory=dict)
    sid_channel: dict[int, str] = field(default_factory=dict)
    sid_seq: dict[int, int] = field(default_factory=dict)
    pending: dict[int, dict[str, Any]] = field(default_factory=dict)  # cmd id -> params
    last_frame_monotonic: float = field(default_factory=time.monotonic)
    gaps: int = 0
    frames: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)


class BookFeed:
    """Maintains ``state.books`` for the subscribed tickers. Pure frame handling lives in
    ``process_frame`` so it is testable without a socket."""

    def __init__(self, ws_url: str, signer: KalshiSigner | None, *, on_fill: FillHandler | None = None,
                 on_lifecycle: LifecycleHandler | None = None, ping_interval: float = 20.0, stale_after: float = 60.0,
                 max_backoff: float = 30.0):
        self.ws_url = ws_url
        self.signer = signer
        self.on_fill = on_fill
        self.on_lifecycle = on_lifecycle
        self.ping_interval = ping_interval
        self.stale_after = stale_after
        self.max_backoff = max_backoff
        self.state = FeedState()
        self._cmd_id = 0
        self._ws: Any = None
        self._tickers: list[str] = []
        self.resync_needed = asyncio.Event()

    # ---- commands ----------------------------------------------------------------------
    def next_id(self) -> int:
        self._cmd_id += 1
        return self._cmd_id

    def subscribe_cmd(self, channel: str, tickers: list[str] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"channels": [channel]}
        if tickers:
            params["market_tickers"] = list(tickers)
        cid = self.next_id()
        cmd = {"id": cid, "cmd": "subscribe", "params": params}
        self.state.pending[cid] = params
        return cmd

    def unsubscribe_cmd(self, sid: int) -> dict[str, Any]:
        return {"id": self.next_id(), "cmd": "unsubscribe", "params": {"sids": [sid]}}

    def update_cmd(self, sid: int, action: str, tickers: list[str]) -> dict[str, Any]:
        return {"id": self.next_id(), "cmd": "update_subscription", "params": {"sids": [sid], "action": action, "market_tickers": list(tickers)}}

    def auth_headers(self) -> dict[str, str]:
        if self.signer is None:
            return {}
        return self.signer.headers("GET", urlsplit(self.ws_url).path)

    # ---- frame processing ----------------------------------------------------------------
    async def process_frame(self, data: Mapping[str, Any]) -> str:
        st = self.state
        st.frames += 1
        st.last_frame_monotonic = time.monotonic()
        mtype = data.get("type")
        if mtype == "subscribed":
            msg = data.get("msg") or {}
            sid, channel = msg.get("sid"), msg.get("channel")
            if sid is None or not channel:
                raise UnexpectedApiResponse("subscribed ack without sid/channel", data)
            st.sid_channel[int(sid)] = str(channel)
            st.sid_seq.pop(int(sid), None)
            st.pending.pop(data.get("id"), None)
            log.info("ws subscribed channel=%s sid=%s", channel, sid)
            return "subscribed"
        if mtype == "error":
            msg = data.get("msg") or {}
            st.errors.append({"id": data.get("id"), "code": msg.get("code"), "msg": msg.get("msg"), "ts": time.time()})
            st.pending.pop(data.get("id"), None)
            log.error("ws error id=%s code=%s msg=%s", data.get("id"), msg.get("code"), msg.get("msg"))
            return "error"
        if mtype in ("ok", "unsubscribed"):
            return mtype
        if mtype in ("orderbook_snapshot", "orderbook_delta"):
            sid = data.get("sid")
            seq = data.get("seq")
            if sid is None or seq is None:
                raise UnexpectedApiResponse(f"{mtype} without sid/seq", data)
            sid, seq = int(sid), int(seq)
            last = st.sid_seq.get(sid)
            if last is not None and seq != last + 1:
                st.gaps += 1
                log.warning("ws sequence gap sid=%s expected=%s got=%s; marking books stale", sid, last + 1, seq)
                for b in st.books.values():
                    if b.sid == sid:
                        b.stale = True
                st.sid_seq[sid] = seq
                self.resync_needed.set()
                if mtype == "orderbook_delta":
                    return "gap"
            st.sid_seq[sid] = seq
            msg = data.get("msg") or {}
            ticker = msg.get("market_ticker")
            if not ticker:
                raise UnexpectedApiResponse(f"{mtype} without market_ticker", data)
            book = st.books.get(ticker)
            if book is None:
                book = st.books[ticker] = OrderBook(ticker)
            book.sid = sid
            if mtype == "orderbook_snapshot":
                yes, no = parse_book_payload(msg)
                book.apply_snapshot(yes, no, seq=seq)
                return "snapshot"
            side = msg.get("side")
            price = dec(msg.get("price_dollars"))
            if price is None:
                cents = dec(msg.get("price"))
                price = cents / 100 if cents is not None else None
            delta = dec(msg.get("delta_fp"))
            if delta is None:
                delta = dec(msg.get("delta"))
            if side not in ("yes", "no") or price is None or delta is None:
                raise UnexpectedApiResponse("orderbook_delta with missing side/price/delta", data)
            if book.seq is None:
                log.warning("delta for %s before any snapshot; marking stale", ticker)
                book.stale = True
                self.resync_needed.set()
                return "delta_without_snapshot"
            book.apply_delta(side, price, delta, seq=seq)
            return "delta"
        if mtype == "fill":
            fill = Fill.parse(data.get("msg") or {})
            log.info("ws fill %s %s %s @ %s fee=%s", fill.book_side, fill.count, fill.ticker, fill.yes_price, fill.fee_cost)
            if self.on_fill:
                r = self.on_fill(fill)
                if asyncio.iscoroutine(r):
                    await r
            return "fill"
        if mtype == "market_lifecycle_v2":
            if self.on_lifecycle:
                r = self.on_lifecycle(dict(data.get("msg") or {}))
                if asyncio.iscoroutine(r):
                    await r
            return "lifecycle"
        if mtype in ("ticker", "trade", "ticker_v2"):
            return mtype
        log.debug("ws unhandled frame type=%s", mtype)
        return "ignored"

    # ---- connection loop -------------------------------------------------------------------
    async def run(self, tickers: list[str], stop: asyncio.Event, subscribe_fills: bool = True) -> None:
        """Connect, subscribe, and process frames until ``stop`` is set. Reconnects with jitter."""
        from websockets.asyncio.client import connect  # imported here so tests need no socket

        self._tickers = list(tickers)
        attempt = 0
        while not stop.is_set():
            try:
                async with connect(self.ws_url, additional_headers=self.auth_headers(), ping_interval=self.ping_interval,
                                   ping_timeout=self.ping_interval, max_size=None) as ws:
                    self._ws = ws
                    attempt = 0
                    self.state.sid_channel.clear()
                    self.state.sid_seq.clear()
                    self.resync_needed.clear()
                    if self._tickers:
                        await ws.send(json.dumps(self.subscribe_cmd("orderbook_delta", self._tickers)))
                    if subscribe_fills and self.signer is not None:
                        await ws.send(json.dumps(self.subscribe_cmd("fill")))
                    log.info("ws connected %s (%d tickers)", self.ws_url, len(self._tickers))
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.stale_after)
                        except asyncio.TimeoutError:
                            log.warning("ws silent for %.0fs; reconnecting", self.stale_after)
                            break
                        data = json.loads(raw)
                        if not isinstance(data, Mapping):
                            raise UnexpectedApiResponse("ws frame is not an object", data)
                        await self.process_frame(data)
                        if self.resync_needed.is_set():
                            log.warning("ws resync requested; reconnecting to rebuild books")
                            break
            except asyncio.CancelledError:
                raise
            except UnexpectedApiResponse:
                raise
            except Exception as e:  # network errors, handshake failures
                log.warning("ws connection error: %s", e)
            finally:
                self._ws = None
            if stop.is_set():
                break
            attempt += 1
            delay = random.uniform(0, min(self.max_backoff, 0.5 * (2 ** attempt)))
            log.info("ws reconnect in %.1fs (attempt %d)", delay, attempt)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def set_tickers(self, tickers: list[str]) -> None:
        """Change the subscription. Simplest robust approach: reconnect with the new list."""
        self._tickers = list(tickers)
        self.resync_needed.set()

    def book(self, ticker: str) -> OrderBook | None:
        return self.state.books.get(ticker)

    def fresh_book(self, ticker: str, max_age: float) -> OrderBook | None:
        b = self.state.books.get(ticker)
        if b is None or b.stale or b.age_seconds() > max_age:
            return None
        return b
