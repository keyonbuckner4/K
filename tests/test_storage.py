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


def test_model_rows_are_written_on_change_or_heartbeat_only(tmp_path):
    from decimal import Decimal

    s = Storage(tmp_path / "t.db", log_heartbeat_sec=600)
    t0 = 1_000_000.0
    for i in range(20):  # the same view every 30 s: one row
        s.log_decision("crypto", "model", False, f"best side bid: net edge -{3.1 + i / 1000:.2f}c < 5c", market_ticker="M", book_side="bid",
                       price=Decimal("0.40"), model_prob=Decimal("0.4231"), ts=t0 + 30 * i)
    assert len(s.decisions()) == 1 and s.suppressed["decisions"] == 19
    s.log_decision("crypto", "model", False, "best side bid: net edge -2.90c < 5c", market_ticker="M", book_side="bid",
                   price=Decimal("0.41"), model_prob=Decimal("0.4231"), ts=t0 + 630)   # price moved a cent: new row
    s.log_decision("crypto", "model", False, "best side bid: net edge -2.90c < 5c", market_ticker="M", book_side="bid",
                   price=Decimal("0.41"), model_prob=Decimal("0.4231"), ts=t0 + 1300)  # heartbeat elapsed: new row
    s.log_decision("crypto", "model", True, "candidate bid 5 @ 0.41: net edge 6c", market_ticker="M", book_side="bid",
                   price=Decimal("0.41"), model_prob=Decimal("0.4231"), ts=t0 + 1301)  # became a candidate: new row
    assert len(s.decisions()) == 4
    for i in range(3):  # execute rows are the audit trail: never throttled
        s.log_decision("crypto", "execute", True, "OBSERVE mode: order not sent", market_ticker="M", ts=t0 + 1302 + i)
    assert len(s.decisions()) == 7
    for i in range(5):  # quotes: same bid/ask, different sizes -> one row
        s.log_quote("M", "0.40", "0.42", str(10 + i), "7", ts=t0 + i)
    s.log_quote("M", "0.40", "0.43", "10", "7", ts=t0 + 6)
    assert s.conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 2 and s.suppressed["quotes"] == 4
    s.close()


def test_latest_model_decisions_and_prune(tmp_path):
    from decimal import Decimal

    s = Storage(tmp_path / "t.db", log_heartbeat_sec=0)  # no throttling: every row lands
    t0 = 2_000_000.0
    for i in range(3):
        s.log_decision("weather", "model", False, "view", market_ticker="A", model_prob=Decimal(f"0.{i + 1}"), price="0.5", ts=t0 + i)
    s.log_decision("weather", "model", False, "view", market_ticker="B", model_prob=Decimal("0.9"), price="0.5", ts=t0 + 5)
    s.log_decision("weather", "gate", False, "not a model row", market_ticker="C", ts=t0 + 6)
    latest = {r["market_ticker"]: r for r in s.latest_model_decisions(since=t0)}
    assert set(latest) == {"A", "B"} and latest["A"]["model_prob"] == "0.3" and latest["A"]["ts"] == t0 + 2
    assert s.latest_model_decisions(since=t0 + 3) and set(r["market_ticker"] for r in s.latest_model_decisions(since=t0 + 3)) == {"B"}
    s.log_quote("A", "0.4", "0.5", "1", "1", ts=t0)
    s.log_quote("A", "0.4", "0.6", "1", "1", ts=t0 + 40 * 86400)
    pruned = s.prune(decisions_days=30, quotes_days=10, now=t0 + 40 * 86400 + 1)
    assert pruned == {"decisions": 5, "quotes": 1}
    assert s.conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1 and s.decisions() == []
    s.close()


def test_compact_keeps_one_row_per_market_per_bucket(tmp_path):
    from decimal import Decimal

    s = Storage(tmp_path / "t.db", log_heartbeat_sec=0)
    t0 = 3_000_000.0
    for i in range(120):  # a flood: every 30 s for an hour, two markets
        for m in ("A", "B"):
            s.log_decision("crypto", "model", False, "view", market_ticker=m, model_prob=Decimal("0.5"), price=Decimal(f"0.{10 + i % 50:02d}"), ts=t0 + 30 * i)
            s.log_quote(m, "0.4", "0.5", "1", "1", ts=t0 + 30 * i)
    s.log_decision("crypto", "execute", True, "OBSERVE mode", market_ticker="A", ts=t0 + 10)
    out = s.compact(bucket_sec=1800)
    assert out["before"]["decisions"] == 241 and out["before"]["quotes"] == 240
    buckets = len({int((t0 + 30 * i) // 1800) for i in range(120)})  # the hour straddles three 30-min buckets here
    assert out["after"]["decisions"] == 2 * buckets + 1 and out["after"]["quotes"] == 2 * buckets   # execute row kept
    kept = s.decisions()
    assert max(r["ts"] for r in kept if r["market_ticker"] == "A" and r["stage"] == "model") == t0 + 30 * 119  # newest row survives
    s.close()
