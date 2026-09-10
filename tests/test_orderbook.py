from decimal import Decimal

import pytest

from kalshi_bot.errors import UnexpectedApiResponse
from kalshi_bot.orderbook import OrderBook, parse_book_payload

D = Decimal


def test_parse_fixed_point_envelope_and_legacy():
    fp = {"orderbook_fp": {"yes_dollars": [["0.4200", "13.00"], ["0.4000", "50.00"]], "no_dollars": [["0.5500", "20.00"]]},
          "orderbook": {"yes": [[42, 13], [40, 50]], "no": [[55, 20]]}}
    yes, no = parse_book_payload(fp)
    assert yes == [(D("0.4200"), D("13.00")), (D("0.4000"), D("50.00"))]
    assert no == [(D("0.5500"), D("20.00"))]
    legacy_yes, legacy_no = parse_book_payload({"orderbook": {"yes": [[42, 13]], "no": [[55, 20]]}})
    assert legacy_yes == [(D("0.42"), D("13"))] and legacy_no == [(D("0.55"), D("20"))]
    ws_yes, ws_no = parse_book_payload({"market_ticker": "X", "yes_dollars_fp": [["0.10", "5"]], "no_dollars_fp": [[0.2, 7]]})
    assert ws_yes == [(D("0.10"), D("5"))] and ws_no == [(D("0.2"), D("7"))]
    assert parse_book_payload({"orderbook": {}}) == ([], [])
    with pytest.raises(UnexpectedApiResponse):
        parse_book_payload({"orderbook": {"yes": "nope"}})


def test_asks_are_complements_of_opposite_bids():
    b = OrderBook.from_payload("T", {"orderbook_fp": {"yes_dollars": [["0.42", "13"], ["0.40", "50"]], "no_dollars": [["0.55", "20"], ["0.50", "9"]]}})
    assert b.best_yes_bid == D("0.42")
    assert b.best_no_bid == D("0.55")
    assert b.best_yes_ask == D("0.45")
    assert b.best_no_ask == D("0.58")
    assert b.yes_ask_size == D("20") and b.yes_bid_size == D("13")
    assert b.spread_cents == D("3.00")
    assert b.mid == D("0.435")
    assert not b.is_crossed()


def test_depth_and_walks():
    b = OrderBook.from_payload("T", {"orderbook_fp": {"yes_dollars": [["0.42", "13"], ["0.40", "50"]], "no_dollars": [["0.55", "20"], ["0.50", "9"]]}})
    assert b.yes_available_to_buy(D("0.45")) == D("20")
    assert b.yes_available_to_buy(D("0.50")) == D("29")
    assert b.yes_available_to_sell(D("0.41")) == D("13")
    assert b.no_available_to_buy(D("0.58")) == D("13")
    avg, total = b.cost_to_buy_yes(D("25"))
    assert total == D("20") * D("0.45") + D("5") * D("0.50")
    assert b.cost_to_buy_yes(D("100")) is None
    avg, total = b.proceeds_to_sell_yes(D("20"))
    assert total == D("13") * D("0.42") + D("7") * D("0.40")


def test_deltas_accumulate_and_remove_levels():
    b = OrderBook("T")
    b.apply_snapshot([(D("0.42"), D("13"))], [(D("0.55"), D("20"))], seq=1)
    b.apply_delta("yes", D("0.42"), D("-13"), seq=2)
    assert b.best_yes_bid is None and b.seq == 2
    b.apply_delta("no", D("0.60"), D("4.5"), seq=3)
    assert b.best_no_bid == D("0.60") and b.best_yes_ask == D("0.40")
    b.apply_delta("no", D("0.60"), D("-4.4999999"), seq=4)
    assert D("0.60") not in b.no_bids  # nets to ~0, removed
    with pytest.raises(UnexpectedApiResponse):
        b.apply_delta("maybe", D("0.5"), D("1"))


def test_to_dict_and_crossed():
    b = OrderBook("T")
    b.apply_snapshot([(D("0.60"), D("1"))], [(D("0.50"), D("1"))])
    assert b.is_crossed()
    d = b.to_dict()
    assert d["yes_bid"] == "0.60" and d["yes_ask"] == "0.50" and d["stale"] is False
