import json
from decimal import Decimal

from kalshi_bot.storage import Storage


def test_decisions_and_state_persist_across_reopen(tmp_path):
    db = tmp_path / "x.db"
    s = Storage(db)
    s.log_decision("ladder_arb", "gate", False, "spread 5c > 3c", market_ticker="A-1-X", price=Decimal("0.45"), edge_net_cents=Decimal("-1"))
    s.set_state("day_key", "2026-09-10")
    s.set_state("day_baseline_cents", 12345)
    s.close()
    s2 = Storage(db)
    rows = s2.decisions()
    assert rows[0]["reason"] == "spread 5c > 3c" and rows[0]["accepted"] == 0 and rows[0]["price"] == "0.45"
    assert s2.get_state("day_key") == "2026-09-10" and s2.get_state("day_baseline_cents") == 12345
    assert s2.get_state("missing", "d") == "d"


def test_intents_orders_fills(tmp_path):
    s = Storage(tmp_path / "x.db")
    legs = [{"ticker": "A-1-X", "book_side": "bid", "price": "0.45", "count": 5}]
    s.save_intent("i1", "ladder_arb", "A-1", "placed", "trade", Decimal("6.5"), 225, legs)
    s.save_order("o1", "c1", "i1", "A-1-X", "bid", Decimal("0.45"), 5, "immediate_or_cancel", "executed", fill_count=5, remaining_count=0)
    assert s.save_fill("f1", "o1", "A-1-X", "bid", "yes", Decimal("0.45"), 5, Decimal("0.02"), True)
    assert not s.save_fill("f1", "o1", "A-1-X", "bid", "yes", Decimal("0.45"), 5, Decimal("0.02"), True)  # idempotent
    s.update_intent("i1", "filled", {"cost": 225})
    it = s.intents()[0]
    assert it["status"] == "filled" and it["legs"] == legs and json.loads(it["result"]) == {"cost": 225}
    assert s.intent_for_market("A-1-X") == "i1" and s.intent_for_market("Z") is None
    assert s.orders("i1")[0]["fill_count"] == "5"
    assert s.fills()[0]["is_taker"] == 1


def test_gaps_results_and_caches(tmp_path):
    s = Storage(tmp_path / "x.db")
    s.log_arb_gap("A-1", "sum_yes_asks", 4, Decimal("0.93"), Decimal("7"), Decimal("4.2"), Decimal("2.8"), 5, [{"t": "x"}])
    assert s.arb_gaps()[0]["kind"] == "sum_yes_asks"
    s.cache_series("KXHIGHNY", {"ticker": "KXHIGHNY", "fee_type": "quadratic"})
    assert s.cached_series("KXHIGHNY", 60)["fee_type"] == "quadratic"
    assert s.cached_series("KXHIGHNY", -1) is None
    s.log_decision("weather", "model", True, "ok", market_ticker="A-1-X", model_prob=Decimal("0.6"))
    assert s.unresolved_decision_markets() == ["A-1-X"]
    s.save_market_result("A-1-X", "A-1", "yes", 1.0)
    assert s.unresolved_decision_markets() == []
    assert s.market_results(["A-1-X"]) == {"A-1-X": "yes"}
    s.snapshot_equity(100, 50, 10)
    assert s.equity_history()[0]["equity_cents"] == 150


def test_intents_for_markets_and_close(tmp_path):
    s = Storage(tmp_path / "x.db")
    s.save_intent("basket", "ladder_arb", "E", "filled", "trade", 0, 0, [{"ticker": "E-A"}, {"ticker": "E-B"}])
    s.save_intent("solo", "weather", "E", "filled", "trade", 0, 0, [{"ticker": "E-A"}])
    s.save_intent("old", "weather", "E", "unwound", "trade", 0, 0, [{"ticker": "E-A"}])
    m = s.intents_for_markets(["E-A", "E-B", "E-C"])
    assert m == {"E-A": {"basket", "solo"}, "E-B": {"basket"}, "E-C": set()}
    assert s.close_intents_for_market("E-A") == 2
    assert s.intents_for_markets(["E-A"]) == {"E-A": set()}
