"""Plain dataclasses for Kalshi objects plus tolerant parsers.

Kalshi completed a fixed-point migration in 2026: prices arrive as dollar strings with up to
four decimals (``yes_bid_dollars: "0.4200"``) and counts as fixed-point strings
(``count_fp: "13.00"``). Legacy integer-cent fields may still be present. Parsers here prefer
the fixed-point fields and fall back to legacy ones, and never invent a value: a missing field
is ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .errors import UnexpectedApiResponse

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")
BOOK_SIDES = ("bid", "ask")
OUTCOME_SIDES = ("yes", "no")


def dec(value: Any, default: Decimal | None = None) -> Decimal | None:
    """Parse a Decimal from str/int/float/Decimal. ``None``/empty -> default."""
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise UnexpectedApiResponse("boolean where a number was expected", value)
    try:
        return Decimal(str(value))
    except InvalidOperation as e:
        raise UnexpectedApiResponse(f"unparseable decimal {value!r}") from e


def price_from(obj: Mapping[str, Any], name: str) -> Decimal | None:
    """Prefer ``{name}_dollars`` (dollar string); fall back to ``{name}`` as integer cents."""
    dollars = obj.get(f"{name}_dollars")
    if dollars not in (None, ""):
        return dec(dollars)
    legacy = obj.get(name)
    if legacy in (None, ""):
        return None
    d = dec(legacy)
    return d / 100 if d is not None else None


def count_from(obj: Mapping[str, Any], name: str) -> Decimal | None:
    fp = obj.get(f"{name}_fp")
    if fp not in (None, ""):
        return dec(fp)
    return dec(obj.get(name))


def parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # Kalshi uses unix seconds for *_ts fields and milliseconds for *_ms fields.
        v = float(value)
        if v > 1e12:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(value)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise UnexpectedApiResponse(f"unparseable timestamp {value!r}") from e
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_price(price: Decimal) -> str:
    """Wire format for V2 order prices: dollar string with four decimals."""
    return f"{Decimal(price).quantize(Decimal('0.0001')):.4f}"


def fmt_count(count: int | Decimal) -> str:
    """Wire format for V2 order counts: fixed-point string with two decimals."""
    return f"{Decimal(count).quantize(CENT):.2f}"


def to_cents(dollars: Decimal | None) -> int | None:
    if dollars is None:
        return None
    return int((Decimal(dollars) * 100).quantize(Decimal("1")))


def series_ticker_of(event_ticker: str | None, market_ticker: str | None = None) -> str | None:
    """Kalshi tickers nest as SERIES-EVENT-MARKET, e.g. KXHIGHNY-26SEP10-B70."""
    t = event_ticker or market_ticker
    if not t:
        return None
    return t.split("-", 1)[0]


@dataclass(frozen=True)
class Market:
    ticker: str
    event_ticker: str
    status: str
    title: str | None
    yes_sub_title: str | None
    market_type: str | None
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    no_bid: Decimal | None
    no_ask: Decimal | None
    last_price: Decimal | None
    yes_bid_size: Decimal | None
    yes_ask_size: Decimal | None
    volume: Decimal | None
    open_interest: Decimal | None
    liquidity: Decimal | None
    open_time: datetime | None
    close_time: datetime | None
    expiration_time: datetime | None
    expected_expiration_time: datetime | None
    settlement_timer_seconds: int | None
    result: str | None
    strike_type: str | None
    floor_strike: Decimal | None
    cap_strike: Decimal | None
    category: str | None
    rules_primary: str | None
    fractional_trading_enabled: bool | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def series_ticker(self) -> str | None:
        return self.raw.get("series_ticker") or series_ticker_of(self.event_ticker, self.ticker)

    @property
    def settle_time(self) -> datetime | None:
        """Best estimate of when the market stops trading: close_time (trading halt) first."""
        return self.close_time or self.expected_expiration_time or self.expiration_time

    def is_open(self) -> bool:
        return self.status == "open"

    @staticmethod
    def parse(d: Mapping[str, Any]) -> "Market":
        if not isinstance(d, Mapping) or "ticker" not in d:
            raise UnexpectedApiResponse("market object without ticker", d)
        sts = d.get("settlement_timer_seconds")
        return Market(
            ticker=str(d["ticker"]),
            event_ticker=str(d.get("event_ticker") or ""),
            status=str(d.get("status") or ""),
            title=d.get("title"),
            yes_sub_title=d.get("yes_sub_title"),
            market_type=d.get("market_type"),
            yes_bid=price_from(d, "yes_bid"),
            yes_ask=price_from(d, "yes_ask"),
            no_bid=price_from(d, "no_bid"),
            no_ask=price_from(d, "no_ask"),
            last_price=price_from(d, "last_price"),
            yes_bid_size=count_from(d, "yes_bid_size"),
            yes_ask_size=count_from(d, "yes_ask_size"),
            volume=count_from(d, "volume"),
            open_interest=count_from(d, "open_interest"),
            liquidity=price_from(d, "liquidity"),
            open_time=parse_time(d.get("open_time")),
            close_time=parse_time(d.get("close_time")),
            expiration_time=parse_time(d.get("expiration_time")),
            expected_expiration_time=parse_time(d.get("expected_expiration_time")),
            settlement_timer_seconds=int(sts) if sts is not None else None,
            result=d.get("result") or None,
            strike_type=d.get("strike_type"),
            floor_strike=dec(d.get("floor_strike")),
            cap_strike=dec(d.get("cap_strike")),
            category=d.get("category"),
            rules_primary=d.get("rules_primary"),
            fractional_trading_enabled=d.get("fractional_trading_enabled"),
            raw=dict(d),
        )


@dataclass(frozen=True)
class Event:
    event_ticker: str
    series_ticker: str
    title: str | None
    category: str | None
    mutually_exclusive: bool | None
    collateral_return_type: str | None
    strike_date: datetime | None
    fee_type_override: str | None
    fee_multiplier_override: Decimal | None
    markets: tuple[Market, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @staticmethod
    def parse(d: Mapping[str, Any]) -> "Event":
        if not isinstance(d, Mapping) or "event_ticker" not in d:
            raise UnexpectedApiResponse("event object without event_ticker", d)
        markets = tuple(Market.parse(m) for m in (d.get("markets") or []))
        return Event(
            event_ticker=str(d["event_ticker"]),
            series_ticker=str(d.get("series_ticker") or series_ticker_of(str(d["event_ticker"])) or ""),
            title=d.get("title"),
            category=d.get("category"),
            mutually_exclusive=d.get("mutually_exclusive"),
            collateral_return_type=d.get("collateral_return_type"),
            strike_date=parse_time(d.get("strike_date")),
            fee_type_override=d.get("fee_type_override"),
            fee_multiplier_override=dec(d.get("fee_multiplier_override")),
            markets=markets,
            raw=dict(d),
        )


@dataclass(frozen=True)
class Series:
    ticker: str
    title: str | None
    category: str | None
    frequency: str | None
    fee_type: str | None
    fee_multiplier: Decimal | None
    settlement_sources: tuple[dict[str, Any], ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @staticmethod
    def parse(d: Mapping[str, Any]) -> "Series":
        if not isinstance(d, Mapping) or "ticker" not in d:
            raise UnexpectedApiResponse("series object without ticker", d)
        return Series(
            ticker=str(d["ticker"]),
            title=d.get("title"),
            category=d.get("category"),
            frequency=d.get("frequency"),
            fee_type=d.get("fee_type"),
            fee_multiplier=dec(d.get("fee_multiplier")),
            settlement_sources=tuple(d.get("settlement_sources") or []),
            raw=dict(d),
        )


@dataclass(frozen=True)
class Balance:
    balance_cents: int
    portfolio_value_cents: int
    updated: datetime | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def equity_cents(self) -> int:
        return self.balance_cents + self.portfolio_value_cents

    @staticmethod
    def parse(d: Mapping[str, Any]) -> "Balance":
        if not isinstance(d, Mapping) or "balance" not in d:
            raise UnexpectedApiResponse("balance response without 'balance'", d)
        bal = d.get("balance_dollars")
        balance_cents = to_cents(dec(bal)) if bal not in (None, "") else int(dec(d["balance"]))
        pv = d.get("portfolio_value")
        portfolio_cents = int(dec(pv)) if pv not in (None, "") else 0
        return Balance(
            balance_cents=int(balance_cents),
            portfolio_value_cents=portfolio_cents,
            updated=parse_time(d.get("updated_ts")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class Position:
    ticker: str
    position: Decimal  # signed contracts: positive = YES held, negative = NO held
    market_exposure: Decimal | None
    realized_pnl: Decimal | None
    fees_paid: Decimal | None
    total_traded: Decimal | None
    resting_orders_count: int | None
    last_updated: datetime | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def event_ticker(self) -> str | None:
        et = self.raw.get("event_ticker")
        if et:
            return str(et)
        parts = self.ticker.split("-")
        return "-".join(parts[:2]) if len(parts) >= 3 else None

    @property
    def outcome_side(self) -> str | None:
        if self.position > 0:
            return "yes"
        if self.position < 0:
            return "no"
        return None

    @staticmethod
    def parse(d: Mapping[str, Any]) -> "Position":
        if not isinstance(d, Mapping) or "ticker" not in d:
            raise UnexpectedApiResponse("position object without ticker", d)
        pos = count_from(d, "position")
        if pos is None:
            raise UnexpectedApiResponse("position object without position/position_fp", d)
        roc = d.get("resting_orders_count")
        return Position(
            ticker=str(d["ticker"]),
            position=pos,
            market_exposure=price_from(d, "market_exposure"),
            realized_pnl=price_from(d, "realized_pnl"),
            fees_paid=price_from(d, "fees_paid"),
            total_traded=price_from(d, "total_traded"),
            resting_orders_count=int(roc) if roc is not None else None,
            last_updated=parse_time(d.get("last_updated_ts")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class Order:
    order_id: str
    ticker: str
    status: str
    book_side: str | None  # bid | ask (YES-referenced)
    yes_price: Decimal | None
    count: Decimal | None
    remaining_count: Decimal | None
    fill_count: Decimal | None
    taker_fees: Decimal | None
    maker_fees: Decimal | None
    client_order_id: str | None
    created: datetime | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @staticmethod
    def parse(d: Mapping[str, Any]) -> "Order":
        if not isinstance(d, Mapping) or "order_id" not in d:
            raise UnexpectedApiResponse("order object without order_id", d)
        book_side = d.get("book_side")
        if book_side is None and d.get("side") in BOOK_SIDES:
            book_side = d.get("side")
        return Order(
            order_id=str(d["order_id"]),
            ticker=str(d.get("ticker") or d.get("market_ticker") or ""),
            status=str(d.get("status") or ""),
            book_side=book_side,
            yes_price=price_from(d, "yes_price"),
            count=count_from(d, "initial_count") or count_from(d, "count"),
            remaining_count=count_from(d, "remaining_count"),
            fill_count=count_from(d, "fill_count"),
            taker_fees=price_from(d, "taker_fees"),
            maker_fees=price_from(d, "maker_fees"),
            client_order_id=d.get("client_order_id"),
            created=parse_time(d.get("created_time")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class OrderAck:
    """Response of ``POST /portfolio/events/orders`` (flat, no wrapper)."""

    order_id: str
    client_order_id: str | None
    fill_count: Decimal
    remaining_count: Decimal
    average_fill_price: Decimal | None
    average_fee_paid: Decimal | None
    ts_ms: int | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @staticmethod
    def parse(d: Mapping[str, Any]) -> "OrderAck":
        if not isinstance(d, Mapping):
            raise UnexpectedApiResponse("order ack is not an object", d)
        body = d.get("order") if isinstance(d.get("order"), Mapping) else d
        if "order_id" not in body:
            raise UnexpectedApiResponse("order ack without order_id", d)
        fill = count_from(body, "fill_count")
        remaining = count_from(body, "remaining_count")
        if fill is None or remaining is None:
            raise UnexpectedApiResponse("order ack without fill_count/remaining_count", d)
        ts = body.get("ts_ms")
        return OrderAck(
            order_id=str(body["order_id"]),
            client_order_id=body.get("client_order_id"),
            fill_count=fill,
            remaining_count=remaining,
            average_fill_price=dec(body.get("average_fill_price")),
            average_fee_paid=dec(body.get("average_fee_paid")),
            ts_ms=int(ts) if ts is not None else None,
            raw=dict(d),
        )


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str | None
    ticker: str
    book_side: str | None
    outcome_side: str | None
    yes_price: Decimal | None
    count: Decimal | None
    fee_cost: Decimal | None
    is_taker: bool | None
    created: datetime | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @staticmethod
    def parse(d: Mapping[str, Any]) -> "Fill":
        if not isinstance(d, Mapping):
            raise UnexpectedApiResponse("fill is not an object", d)
        fid = d.get("fill_id") or d.get("trade_id")
        if not fid:
            raise UnexpectedApiResponse("fill without fill_id/trade_id", d)
        ticker = d.get("ticker") or d.get("market_ticker")
        return Fill(
            fill_id=str(fid),
            order_id=d.get("order_id"),
            ticker=str(ticker or ""),
            book_side=d.get("book_side"),
            outcome_side=d.get("outcome_side") or (d.get("side") if d.get("side") in OUTCOME_SIDES else None),
            yes_price=price_from(d, "yes_price"),
            count=count_from(d, "count"),
            fee_cost=price_from(d, "fee_cost"),
            is_taker=d.get("is_taker"),
            created=parse_time(d.get("created_time") or d.get("ts_ms") or d.get("ts")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class Settlement:
    ticker: str
    event_ticker: str | None
    market_result: str | None
    revenue_cents: int | None
    fee_cost: Decimal | None
    settled: datetime | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @staticmethod
    def parse(d: Mapping[str, Any]) -> "Settlement":
        if not isinstance(d, Mapping) or "ticker" not in d:
            raise UnexpectedApiResponse("settlement without ticker", d)
        rev = d.get("revenue")
        return Settlement(
            ticker=str(d["ticker"]),
            event_ticker=d.get("event_ticker"),
            market_result=d.get("market_result"),
            revenue_cents=int(dec(rev)) if rev not in (None, "") else None,
            fee_cost=price_from(d, "fee_cost"),
            settled=parse_time(d.get("settled_time")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class ExchangeStatus:
    exchange_active: bool
    trading_active: bool
    estimated_resume: datetime | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @staticmethod
    def parse(d: Mapping[str, Any]) -> "ExchangeStatus":
        if not isinstance(d, Mapping) or "trading_active" not in d:
            raise UnexpectedApiResponse("exchange status without trading_active", d)
        return ExchangeStatus(
            exchange_active=bool(d.get("exchange_active")),
            trading_active=bool(d.get("trading_active")),
            estimated_resume=parse_time(d.get("exchange_estimated_resume_time")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class RateBudget:
    refill_per_sec: int
    capacity: int


@dataclass(frozen=True)
class AccountLimits:
    """``GET /account/limits``.

    Live servers return nested token buckets (``read``/``write`` with ``refill_rate`` and
    ``bucket_capacity``, in tokens per second). Older spec text described flat ``read_limit`` /
    ``write_limit`` integers (requests per second). Both are accepted; anything else raises.
    """

    usage_tier: str | None
    read: RateBudget
    write: RateBudget
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @staticmethod
    def parse(d: Mapping[str, Any], default_cost: int = 10) -> "AccountLimits":
        if not isinstance(d, Mapping):
            raise UnexpectedApiResponse("account limits is not an object", d)
        budgets: dict[str, RateBudget] = {}
        for kind in ("read", "write"):
            node = d.get(kind)
            if isinstance(node, Mapping) and "refill_rate" in node:
                refill = int(node["refill_rate"])
                cap = int(node.get("bucket_capacity") or refill)
                budgets[kind] = RateBudget(refill_per_sec=refill, capacity=cap)
                continue
            flat = d.get(f"{kind}_limit")
            if flat is not None:
                refill = int(flat) * default_cost  # requests/s -> tokens/s
                budgets[kind] = RateBudget(refill_per_sec=refill, capacity=refill)
                continue
            raise UnexpectedApiResponse(f"account limits without a recognizable '{kind}' budget", d)
        return AccountLimits(usage_tier=d.get("usage_tier"), read=budgets["read"], write=budgets["write"], raw=dict(d))
