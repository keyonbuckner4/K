import asyncio
from datetime import timedelta
from decimal import Decimal

from kalshi_bot.fees import FeeSchedule
from kalshi_bot.gate import GateConfig
from kalshi_bot.models import Event, Series
from kalshi_bot.orderbook import OrderBook
from kalshi_bot.storage import Storage
from kalshi_bot.strategies.base import ScanContext
from kalshi_bot.strategies.ladder_arb import LadderArbStrategy, Rung, is_exhaustive_ladder
from kalshi_bot.pricing import market_condition

from helpers import NOW, market


def fees():
    f = FeeSchedule()
    f.register_series(Series.parse({"ticker": "KXHIGHNY", "fee_type": "quadratic", "fee_multiplier": "0.07"}))
    return f


def ladder_event(prices, exclusive=True):
    """prices: list of (yes_bid, yes_ask) for rungs <=69, 70-71, 72-73, >=74."""
    specs = [("T1", "less_or_equal", None, "69"), ("T2", "between", "70", "71"), ("T3", "between", "72", "73"), ("T4", "greater_or_equal", "74", None)]
    markets, books = [], {}
    for (t, st, fl, cp), (bid, ask) in zip(specs, prices):
        m = market(ticker=f"KXHIGHNY-26SEP10-{t}", strike_type=st, floor=fl, cap=cp, yes_bid=bid, yes_ask=ask)
        markets.append(m.raw)
        books[m.ticker] = OrderBook.from_payload(m.ticker, {"orderbook_fp": {"yes_dollars": [[bid, "50"]], "no_dollars": [[str(round(1 - float(ask), 2)), "50"]]}})
    ev = Event.parse({"event_ticker": "KXHIGHNY-26SEP10", "series_ticker": "KXHIGHNY", "mutually_exclusive": exclusive, "category": "Climate and Weather", "markets": markets})
    return ev, books


def ctx(tmp_path, ev, books):
    return ScanContext(NOW, [ev], books, fees(), GateConfig(), Storage(tmp_path / "x.db"))


def test_exhaustive_ladder_detection():
    ev, books = ladder_event([("0.10", "0.12"), ("0.30", "0.33"), ("0.30", "0.33"), ("0.15", "0.18")])
    rungs = [Rung(m, market_condition(m), books[m.ticker]) for m in ev.markets]
    assert is_exhaustive_ladder(rungs)
    assert not is_exhaustive_ladder(rungs[1:])          # no lower tail
    assert not is_exhaustive_ladder(rungs[:1] + rungs[2:])  # hole at 70-71


def test_buy_all_yes_when_asks_sum_below_one(tmp_path):
    # asks: 0.12 + 0.20 + 0.30 + 0.18 = 0.80 -> 20c gross; fees ~ small -> tradeable
    ev, books = ladder_event([("0.10", "0.12"), ("0.18", "0.20"), ("0.28", "0.30"), ("0.15", "0.18")])
    c = ctx(tmp_path, ev, books)
    intents = asyncio.run(LadderArbStrategy({"max_contracts_per_leg": 5}, c.storage).scan(c))
    basket = [i for i in intents if i.reason.startswith("sum_yes_asks_below_1")]
    assert len(basket) == 1
    it = basket[0]
    assert [l.book_side for l in it.legs] == ["bid"] * 4 and all(l.count == 5 for l in it.legs)
    assert it.expected_edge_cents > Decimal("15")
    gaps = c.storage.arb_gaps()
    assert any(g["kind"] == "sum_yes_asks_below_1" and Decimal(g["net_edge_cents"]) > 15 for g in gaps)


def test_sell_all_yes_when_bids_sum_above_one(tmp_path):
    ev, books = ladder_event([("0.30", "0.32"), ("0.35", "0.37"), ("0.30", "0.32"), ("0.25", "0.27")])  # bids sum 1.20
    c = ctx(tmp_path, ev, books)
    intents = asyncio.run(LadderArbStrategy({}, c.storage).scan(c))
    it = next(i for i in intents if i.reason.startswith("sum_yes_bids_above_1"))
    assert all(l.book_side == "ask" for l in it.legs)
    assert it.expected_edge_cents > Decimal("10")
    assert it.max_cost_cents == sum(int((Decimal("1") - l.price) * l.count * 100) for l in it.legs)


def test_fair_ladder_logs_no_gap_and_small_gap_is_logged_but_rejected(tmp_path):
    ev, books = ladder_event([("0.10", "0.12"), ("0.30", "0.33"), ("0.30", "0.33"), ("0.20", "0.22")])  # asks sum exactly 1.00
    c = ctx(tmp_path, ev, books)
    assert asyncio.run(LadderArbStrategy({}, c.storage).scan(c)) == []
    assert c.storage.arb_gaps() == []
    ev, books = ladder_event([("0.10", "0.12"), ("0.30", "0.33"), ("0.30", "0.33"), ("0.18", "0.20")])  # asks sum 0.98 -> 2c gross
    c = ctx(tmp_path, ev, books)
    assert asyncio.run(LadderArbStrategy({}, c.storage).scan(c)) == []
    gaps = c.storage.arb_gaps()
    assert len(gaps) == 1 and Decimal(gaps[0]["gross_edge_cents"]) == 2 and Decimal(gaps[0]["net_edge_cents"]) < 0
    assert any("net" in d["reason"] and d["accepted"] == 0 for d in c.storage.decisions())


def test_non_exhaustive_or_non_exclusive_events_skip_sum_arb(tmp_path):
    ev, books = ladder_event([("0.10", "0.12"), ("0.18", "0.20"), ("0.28", "0.30"), ("0.15", "0.18")], exclusive=False)
    c = ctx(tmp_path, ev, books)
    assert [i for i in asyncio.run(LadderArbStrategy({}, c.storage).scan(c)) if "sum_yes" in i.reason] == []


def test_threshold_monotonicity_arb(tmp_path):
    # nested "above X" markets: above 100 should cost >= above 110. Here above-100 asks 0.40 but above-110 bids 0.50.
    m1 = market(ticker="KXHIGHNY-26SEP10-A100", strike_type="greater", floor="100", cap=None, yes_bid="0.38", yes_ask="0.40")
    m2 = market(ticker="KXHIGHNY-26SEP10-A110", strike_type="greater", floor="110", cap=None, yes_bid="0.50", yes_ask="0.52")
    books = {m1.ticker: OrderBook.from_payload(m1.ticker, {"orderbook_fp": {"yes_dollars": [["0.38", "50"]], "no_dollars": [["0.60", "50"]]}}),
             m2.ticker: OrderBook.from_payload(m2.ticker, {"orderbook_fp": {"yes_dollars": [["0.50", "50"]], "no_dollars": [["0.48", "50"]]}})}
    ev = Event.parse({"event_ticker": "KXHIGHNY-26SEP10", "series_ticker": "KXHIGHNY", "mutually_exclusive": False, "markets": [m1.raw, m2.raw]})
    c = ctx(tmp_path, ev, books)
    intents = asyncio.run(LadderArbStrategy({}, c.storage).scan(c))
    it = next(i for i in intents if i.reason.startswith("threshold_monotonicity"))
    assert [(l.ticker, l.book_side, str(l.price)) for l in it.legs] == [(m1.ticker, "bid", "0.40"), (m2.ticker, "ask", "0.50")]
    assert it.expected_edge_cents > Decimal("5")


def test_unknown_fee_series_is_refused(tmp_path):
    ev, books = ladder_event([("0.10", "0.12"), ("0.18", "0.20"), ("0.28", "0.30"), ("0.15", "0.18")])
    c = ScanContext(NOW, [ev], books, FeeSchedule(), GateConfig(), Storage(tmp_path / "x.db"))
    assert asyncio.run(LadderArbStrategy({}, c.storage).scan(c)) == []
    assert "fee parameters unknown" in c.storage.decisions()[0]["reason"]
