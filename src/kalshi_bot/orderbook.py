"""Local order book. Kalshi books are bids-only and YES-referenced.

The REST orderbook and the WebSocket snapshot both carry two lists of bids: YES bids and NO
bids. There are no ask lists: the best YES ask is ``1 - best NO bid`` and vice versa. Prices are
dollars (``Decimal``, four decimals), quantities are contracts (``Decimal``, may be fractional).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .errors import UnexpectedApiResponse
from .models import ONE, ZERO, dec

EPS = Decimal("0.000001")


@dataclass
class Level:
    price: Decimal
    qty: Decimal


def _parse_levels(raw: Any, legacy_cents: bool) -> list[tuple[Decimal, Decimal]]:
    out: list[tuple[Decimal, Decimal]] = []
    if raw is None:
        return out
    if not isinstance(raw, list):
        raise UnexpectedApiResponse("orderbook side is not a list", raw)
    for pair in raw:
        if isinstance(pair, Mapping):  # {"price": ..., "count": ...} style, tolerated
            p, q = pair.get("price_dollars", pair.get("price")), pair.get("count_fp", pair.get("count", pair.get("quantity")))
        elif isinstance(pair, (list, tuple)) and len(pair) >= 2:
            p, q = pair[0], pair[1]
        else:
            raise UnexpectedApiResponse("orderbook level is not a [price, count] pair", pair)
        price = dec(p)
        qty = dec(q)
        if price is None or qty is None:
            raise UnexpectedApiResponse("orderbook level with empty price/count", pair)
        if legacy_cents:
            price = price / 100
        out.append((price, qty))
    return out


def parse_book_payload(payload: Mapping[str, Any]) -> tuple[list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]]:
    """Accept every envelope Kalshi has used: ``orderbook_fp``/``orderbook`` wrappers or bare;
    ``yes_dollars_fp`` / ``yes_dollars`` (dollar strings) or legacy ``yes`` (integer cents)."""
    ob: Mapping[str, Any] = payload
    if isinstance(payload.get("orderbook_fp"), Mapping):
        ob = payload["orderbook_fp"]
    elif isinstance(payload.get("orderbook"), Mapping):
        ob = payload["orderbook"]
    for yes_key, no_key, legacy in (("yes_dollars_fp", "no_dollars_fp", False), ("yes_dollars", "no_dollars", False), ("yes", "no", True)):
        if yes_key in ob or no_key in ob:
            return _parse_levels(ob.get(yes_key), legacy), _parse_levels(ob.get(no_key), legacy)
    if not ob:  # empty book
        return [], []
    raise UnexpectedApiResponse("orderbook payload without yes/no levels", payload)


@dataclass
class OrderBook:
    ticker: str
    yes_bids: dict[Decimal, Decimal] = field(default_factory=dict)
    no_bids: dict[Decimal, Decimal] = field(default_factory=dict)
    seq: int | None = None
    sid: int | None = None
    updated_monotonic: float = field(default_factory=time.monotonic)
    updated_wall: float = field(default_factory=time.time)
    stale: bool = False

    # ---- construction -------------------------------------------------------------
    @classmethod
    def from_payload(cls, ticker: str, payload: Mapping[str, Any]) -> "OrderBook":
        book = cls(ticker)
        yes, no = parse_book_payload(payload)
        book.apply_snapshot(yes, no)
        return book

    def _touch(self) -> None:
        self.updated_monotonic = time.monotonic()
        self.updated_wall = time.time()
        self.stale = False

    def apply_snapshot(self, yes: Iterable[tuple[Decimal, Decimal]], no: Iterable[tuple[Decimal, Decimal]], seq: int | None = None) -> None:
        self.yes_bids = {p: q for p, q in yes if q > EPS}
        self.no_bids = {p: q for p, q in no if q > EPS}
        self.seq = seq
        self._touch()

    def apply_delta(self, side: str, price: Decimal, delta: Decimal, seq: int | None = None) -> None:
        book = self.yes_bids if side == "yes" else self.no_bids if side == "no" else None
        if book is None:
            raise UnexpectedApiResponse(f"orderbook delta with side {side!r}")
        new = book.get(price, ZERO) + delta
        if new <= EPS:
            book.pop(price, None)
        else:
            book[price] = new
        if seq is not None:
            self.seq = seq
        self._touch()

    # ---- top of book ----------------------------------------------------------------
    @property
    def best_yes_bid(self) -> Decimal | None:
        return max(self.yes_bids) if self.yes_bids else None

    @property
    def best_no_bid(self) -> Decimal | None:
        return max(self.no_bids) if self.no_bids else None

    @property
    def best_yes_ask(self) -> Decimal | None:
        nb = self.best_no_bid
        return (ONE - nb) if nb is not None else None

    @property
    def best_no_ask(self) -> Decimal | None:
        yb = self.best_yes_bid
        return (ONE - yb) if yb is not None else None

    @property
    def yes_bid_size(self) -> Decimal:
        b = self.best_yes_bid
        return self.yes_bids[b] if b is not None else ZERO

    @property
    def yes_ask_size(self) -> Decimal:
        b = self.best_no_bid
        return self.no_bids[b] if b is not None else ZERO

    @property
    def no_bid_size(self) -> Decimal:
        return self.yes_ask_size

    @property
    def no_ask_size(self) -> Decimal:
        return self.yes_bid_size

    @property
    def spread_cents(self) -> Decimal | None:
        a, b = self.best_yes_ask, self.best_yes_bid
        return (a - b) * 100 if a is not None and b is not None else None

    @property
    def mid(self) -> Decimal | None:
        a, b = self.best_yes_ask, self.best_yes_bid
        return (a + b) / 2 if a is not None and b is not None else None

    def is_crossed(self) -> bool:
        a, b = self.best_yes_ask, self.best_yes_bid
        return a is not None and b is not None and a < b

    def age_seconds(self) -> float:
        return time.monotonic() - self.updated_monotonic

    # ---- depth ----------------------------------------------------------------------
    def yes_available_to_buy(self, limit_price: Decimal) -> Decimal:
        """Contracts of YES purchasable at or below ``limit_price`` (NO bids at >= 1 - price)."""
        floor = ONE - limit_price
        return sum((q for p, q in self.no_bids.items() if p >= floor), ZERO)

    def yes_available_to_sell(self, limit_price: Decimal) -> Decimal:
        """Contracts of YES sellable at or above ``limit_price`` (YES bids at >= price)."""
        return sum((q for p, q in self.yes_bids.items() if p >= limit_price), ZERO)

    def no_available_to_buy(self, limit_price: Decimal) -> Decimal:
        """Buying NO at n is selling YES at 1 - n."""
        return self.yes_available_to_sell(ONE - limit_price)

    def cost_to_buy_yes(self, count: Decimal) -> tuple[Decimal, Decimal] | None:
        """Walk NO bids (best first). Returns (average YES price, total dollars) or None."""
        remaining = Decimal(count)
        total = ZERO
        for nb in sorted(self.no_bids, reverse=True):
            take = min(remaining, self.no_bids[nb])
            total += take * (ONE - nb)
            remaining -= take
            if remaining <= 0:
                break
        if remaining > 0:
            return None
        return total / Decimal(count), total

    def proceeds_to_sell_yes(self, count: Decimal) -> tuple[Decimal, Decimal] | None:
        remaining = Decimal(count)
        total = ZERO
        for yb in sorted(self.yes_bids, reverse=True):
            take = min(remaining, self.yes_bids[yb])
            total += take * yb
            remaining -= take
            if remaining <= 0:
                break
        if remaining > 0:
            return None
        return total / Decimal(count), total

    def to_dict(self, depth: int = 5) -> dict[str, Any]:
        yes = sorted(self.yes_bids.items(), reverse=True)[:depth]
        no = sorted(self.no_bids.items(), reverse=True)[:depth]
        return {
            "ticker": self.ticker,
            "yes_bid": str(self.best_yes_bid) if self.best_yes_bid is not None else None,
            "yes_ask": str(self.best_yes_ask) if self.best_yes_ask is not None else None,
            "yes_bid_size": str(self.yes_bid_size),
            "yes_ask_size": str(self.yes_ask_size),
            "spread_cents": str(self.spread_cents) if self.spread_cents is not None else None,
            "yes_bids": [[str(p), str(q)] for p, q in yes],
            "no_bids": [[str(p), str(q)] for p, q in no],
            "seq": self.seq,
            "age_sec": round(self.age_seconds(), 2),
            "stale": self.stale,
        }
