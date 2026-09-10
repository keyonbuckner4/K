from decimal import Decimal

import pytest

from kalshi_bot import fees
from kalshi_bot.errors import DataUnavailable, UnexpectedApiResponse
from kalshi_bot.models import Series


def test_quadratic_fee_matches_published_examples():
    # 100 contracts at 50c on the general 0.07 schedule: 0.07 * 100 * 0.25 = $1.75
    assert fees.quadratic_fee(100, Decimal("0.50"), Decimal("0.07")) == Decimal("1.75")
    # rounds UP to the next cent per order
    assert fees.quadratic_fee(1, Decimal("0.50"), Decimal("0.07")) == Decimal("0.02")
    # 20c: 0.07 * 0.2 * 0.8 = 0.0112 per contract -> 100 contracts = $1.12
    assert fees.quadratic_fee(100, Decimal("0.20"), Decimal("0.07")) == Decimal("1.12")
    # fee vanishes at the extremes
    assert fees.quadratic_fee(100, Decimal("0.99"), Decimal("0.07")) == Decimal("0.07")
    assert fees.quadratic_fee(0, Decimal("0.5"), Decimal("0.07")) == Decimal("0.00")


def test_fee_is_largest_at_fifty_cents():
    f = lambda p: fees.quadratic_fee(1000, Decimal(p), Decimal("0.07"))
    assert f("0.50") > f("0.30") > f("0.10")
    assert f("0.50") > f("0.70") > f("0.90")


def test_schedule_uses_series_parameters_from_api():
    sched = fees.FeeSchedule()
    s = Series.parse({"ticker": "KXHIGHNY", "fee_type": "quadratic", "fee_multiplier": "0.07"})
    sched.register_series(s)
    assert sched.fee("KXHIGHNY", 10, Decimal("0.50")) == Decimal("0.18")  # 0.175 -> 0.18
    assert sched.fee("KXHIGHNY", 10, Decimal("0.50"), taker=False) == Decimal("0.00")
    assert sched.fee_cents_per_contract("KXHIGHNY", 10, Decimal("0.50")) == Decimal("1.8")


def test_maker_fee_series():
    sched = fees.FeeSchedule()
    sched.register_series(Series.parse({"ticker": "KXBTCD", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": "0.07"}))
    assert sched.fee("KXBTCD", 100, Decimal("0.50"), taker=False) == Decimal("0.44")  # 0.4375 -> 0.44
    assert sched.fee("KXBTCD", 100, Decimal("0.50"), taker=True) == Decimal("1.75")


def test_unknown_series_and_shapes_fail_loudly():
    sched = fees.FeeSchedule()
    with pytest.raises(DataUnavailable):
        sched.fee("KXNOPE", 1, Decimal("0.5"))
    with pytest.raises(UnexpectedApiResponse):
        sched.register_series(Series.parse({"ticker": "X", "fee_type": "cubic", "fee_multiplier": "0.07"}))
    with pytest.raises(UnexpectedApiResponse):
        sched.register_series(Series.parse({"ticker": "X", "fee_type": "quadratic", "fee_multiplier": "7"}))
    with pytest.raises(UnexpectedApiResponse):
        sched.register_series(Series.parse({"ticker": "X"}))
    with pytest.raises(DataUnavailable):
        sched.register_series(Series.parse({"ticker": "X", "fee_type": "flat", "fee_multiplier": "0"}))


def test_flat_fee_requires_config():
    sched = fees.FeeSchedule.from_config({"flat_fee_per_contract": {"KXFLAT": "0.01"}})
    sched.register_series(Series.parse({"ticker": "KXFLAT", "fee_type": "flat", "fee_multiplier": "0"}))
    assert sched.fee("KXFLAT", 7, Decimal("0.5")) == Decimal("0.07")


def test_config_override_wins():
    sched = fees.FeeSchedule.from_config({"series_overrides": {"KXX": 0.035}})
    assert sched.fee("KXX", 100, Decimal("0.5")) == Decimal("0.88")  # 0.875 -> 0.88
