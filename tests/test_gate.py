from datetime import timedelta
from decimal import Decimal

from kalshi_bot.fees import FeeSchedule
from kalshi_bot.gate import GateConfig, check_intent, check_leg
from kalshi_bot.intent import ARB, Intent, Leg
from kalshi_bot.models import Series
from kalshi_bot.orderbook import OrderBook

from helpers import NOW, market

CFG = GateConfig()


def fees():
    f = FeeSchedule()
    f.register_series(Series.parse({"ticker": "KXHIGHNY", "fee_type": "quadratic", "fee_multiplier": "0.07"}))
    return f


def book(yes_bid="0.42", no_bid="0.55", yes_qty="100", no_qty="100"):
    return OrderBook.from_payload("KXHIGHNY-26SEP10-B70", {"orderbook_fp": {"yes_dollars": [[yes_bid, yes_qty]], "no_dollars": [[no_bid, no_qty]]}})


def leg(prob="0.60", price="0.45", count=5, side="bid"):
    return Leg("KXHIGHNY-26SEP10-B70", side, Decimal(price), count, market=market(), model_prob=Decimal(prob))


def test_all_four_checks_pass():
    r = check_leg(leg(), book(), market(), fees(), NOW, CFG)
    assert r.ok, r.reason
    # gross 15c, fee at 45c for 5 contracts: 0.07*5*0.45*0.55 = 0.0866 -> $0.09 -> 1.8c/contract
    assert Decimal(r.details["edge_gross_cents"]) == Decimal("15") and Decimal(r.details["fee_cents_per_contract"]) == Decimal("1.8")
    assert Decimal(r.details["edge_net_cents"]) == Decimal("13.2")


def test_net_edge_below_five_cents_rejected():
    r = check_leg(leg(prob="0.51"), book(), market(), fees(), NOW, CFG)  # gross 6c, net 4.2c
    assert not r.ok and "net edge" in r.reason
    r = check_leg(leg(prob="0.52"), book(), market(), fees(), NOW, CFG)  # gross 7c, net 5.2c
    assert r.ok


def test_spread_wider_than_three_cents_rejected():
    r = check_leg(leg(), book(yes_bid="0.40"), market(), fees(), NOW, CFG)  # ask 45, bid 40 -> 5c
    assert not r.ok and "spread" in r.reason


def test_depth_must_be_twice_my_size():
    r = check_leg(leg(count=5), book(no_qty="9"), market(), fees(), NOW, CFG)
    assert not r.ok and "resting size" in r.reason
    assert check_leg(leg(count=5), book(no_qty="10"), market(), fees(), NOW, CFG).ok


def test_settlement_inside_ten_minutes_rejected():
    m = market(close=NOW + timedelta(minutes=5))
    r = check_leg(leg(), book(), m, fees(), NOW, CFG)
    assert not r.ok and "settles in" in r.reason


def test_ask_leg_uses_no_side_probability_and_depth():
    # selling YES at 0.42 (buying NO at 0.58): P(no) = 0.75 -> gross = 75 - 58 = 17c
    r = check_leg(leg(prob="0.25", price="0.42", side="ask"), book(), market(), fees(), NOW, CFG)
    assert r.ok and Decimal(r.details["edge_gross_cents"]) == Decimal("17")


def test_missing_inputs_fail_closed():
    assert not check_leg(leg(), None, market(), fees(), NOW, CFG).ok
    assert not check_leg(leg(prob=None) if False else Leg("KXHIGHNY-26SEP10-B70", "bid", Decimal("0.45"), 5, market=market()), book(), market(), fees(), NOW, CFG).ok


def test_arb_basket_checks_edge_at_basket_level():
    it = Intent("ladder_arb", "KXHIGHNY-26SEP10", [Leg("KXHIGHNY-26SEP10-B70", "bid", Decimal("0.45"), 5, market=market())], kind=ARB,
                expected_edge_cents=Decimal("4"))
    results = check_intent(it, {"KXHIGHNY-26SEP10-B70": book()}, fees(), NOW, CFG)
    assert results[0].ok and not results[-1].ok and "basket net edge" in results[-1].reason
    it.expected_edge_cents = Decimal("6")
    assert all(r.ok for r in check_intent(it, {"KXHIGHNY-26SEP10-B70": book()}, fees(), NOW, CFG))
