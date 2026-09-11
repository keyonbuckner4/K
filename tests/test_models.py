from datetime import timezone
from decimal import Decimal

import pytest

from kalshi_bot import models
from kalshi_bot.errors import UnexpectedApiResponse


def test_price_prefers_dollars_then_cents():
    assert models.price_from({"yes_bid_dollars": "0.4200", "yes_bid": 41}, "yes_bid") == Decimal("0.4200")
    assert models.price_from({"yes_bid": 41}, "yes_bid") == Decimal("0.41")
    assert models.price_from({}, "yes_bid") is None


def test_count_prefers_fp():
    assert models.count_from({"count_fp": "13.00", "count": 12}, "count") == Decimal("13.00")
    assert models.count_from({"count": 12}, "count") == Decimal("12")


def test_wire_formats():
    assert models.fmt_price(Decimal("0.45")) == "0.4500"
    assert models.fmt_price(Decimal("0.123456")) == "0.1235"
    assert models.fmt_count(5) == "5.00"
    assert models.to_cents(Decimal("1.75")) == 175


def test_market_parse_fixed_point_and_times():
    m = models.Market.parse({
        "ticker": "KXHIGHNY-26SEP10-B70", "event_ticker": "KXHIGHNY-26SEP10", "status": "open",
        "yes_bid_dollars": "0.4200", "yes_ask_dollars": "0.4500", "no_bid_dollars": "0.5500", "no_ask_dollars": "0.5800",
        "yes_bid_size_fp": "120.00", "yes_ask_size_fp": "80.00", "close_time": "2026-09-10T23:00:00Z",
        "strike_type": "between", "floor_strike": 70, "cap_strike": 71, "settlement_timer_seconds": 300,
        "rules_primary": "If the high temperature at Central Park is 70-71F...",
    })
    assert m.series_ticker == "KXHIGHNY"
    assert m.yes_ask == Decimal("0.4500") and m.yes_bid_size == Decimal("120.00")
    assert m.close_time.tzinfo == timezone.utc and m.close_time.hour == 23
    assert m.floor_strike == Decimal("70") and m.cap_strike == Decimal("71")
    assert m.settle_time == m.close_time
    assert m.is_open()
    assert models.Market.parse({"ticker": "A-1-X", "event_ticker": "A-1", "status": "active"}).is_open()
    assert not models.Market.parse({"ticker": "A-1-X", "event_ticker": "A-1", "status": "closed"}).is_open()
    assert not models.Market.parse({"ticker": "A-1-X", "event_ticker": "A-1"}).is_open()


def test_market_requires_ticker():
    with pytest.raises(UnexpectedApiResponse):
        models.Market.parse({"status": "open"})


def test_balance_parse():
    b = models.Balance.parse({"balance": 12345, "balance_dollars": "123.45", "portfolio_value": 1000, "updated_ts": 1757500000})
    assert b.balance_cents == 12345 and b.portfolio_value_cents == 1000 and b.equity_cents == 13345
    assert b.updated.year == 2025
    b2 = models.Balance.parse({"balance": 500})
    assert b2.balance_cents == 500 and b2.portfolio_value_cents == 0
    with pytest.raises(UnexpectedApiResponse):
        models.Balance.parse({"nope": 1})


def test_position_parse_signed_fp():
    p = models.Position.parse({"ticker": "KXHIGHNY-26SEP10-B70", "position_fp": "-3.00", "market_exposure_dollars": "1.50",
                               "realized_pnl_dollars": "-0.20", "fees_paid_dollars": "0.05", "resting_orders_count": 0,
                               "last_updated_ts": "2026-05-31T23:34:49.912729Z"})
    assert p.position == Decimal("-3.00") and p.outcome_side == "no"
    assert p.event_ticker == "KXHIGHNY-26SEP10"
    assert p.last_updated.year == 2026
    with pytest.raises(UnexpectedApiResponse):
        models.Position.parse({"ticker": "X"})


def test_order_ack_parse_flat_and_wrapped():
    a = models.OrderAck.parse({"order_id": "o1", "client_order_id": "c1", "fill_count": "5.00", "remaining_count": "0.00",
                               "average_fill_price": "0.4500", "average_fee_paid": "0.0200", "ts_ms": 1757500000000})
    assert a.fill_count == Decimal("5.00") and a.average_fill_price == Decimal("0.4500")
    w = models.OrderAck.parse({"order": {"order_id": "o2", "fill_count_fp": "1.00", "remaining_count_fp": "4.00"}})
    assert w.order_id == "o2" and w.remaining_count == Decimal("4.00")
    with pytest.raises(UnexpectedApiResponse):
        models.OrderAck.parse({"order_id": "o3"})


def test_account_limits_both_shapes():
    nested = models.AccountLimits.parse({"usage_tier": "basic", "read": {"refill_rate": 200, "bucket_capacity": 400},
                                         "write": {"refill_rate": 100, "bucket_capacity": 100}, "grants": []})
    assert nested.read.refill_per_sec == 200 and nested.read.capacity == 400 and nested.write.capacity == 100
    flat = models.AccountLimits.parse({"usage_tier": "standard", "read_limit": 20, "write_limit": 10})
    assert flat.read.refill_per_sec == 200 and flat.write.refill_per_sec == 100
    with pytest.raises(UnexpectedApiResponse):
        models.AccountLimits.parse({"usage_tier": "basic"})


def test_fill_and_settlement_parse():
    f = models.Fill.parse({"trade_id": "t1", "fill_id": "f1", "order_id": "o1", "market_ticker": "KXX-1-A", "is_taker": True,
                           "yes_price_dollars": "0.4500", "count_fp": "5.00", "fee_cost_dollars": "0.02", "book_side": "bid",
                           "outcome_side": "yes", "created_time": "2026-09-10T12:00:00Z"})
    assert f.ticker == "KXX-1-A" and f.book_side == "bid" and f.count == Decimal("5.00")
    s = models.Settlement.parse({"ticker": "KXX-1-A", "event_ticker": "KXX-1", "market_result": "yes", "revenue": 500,
                                 "settled_time": "2026-09-11T00:00:00Z", "fee_cost_dollars": "0.00"})
    assert s.revenue_cents == 500 and s.market_result == "yes"


def test_exchange_status():
    s = models.ExchangeStatus.parse({"exchange_active": True, "trading_active": False})
    assert s.exchange_active and not s.trading_active
    with pytest.raises(UnexpectedApiResponse):
        models.ExchangeStatus.parse({})
