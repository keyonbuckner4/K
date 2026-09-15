import http.client
import json
import time
from decimal import Decimal

import pytest

from kalshi_bot.backtest import MODEL_VERSION, run_backtest
from kalshi_bot.dashboard import Dashboard, render, status_payload
from kalshi_bot.review import weekly_review
from kalshi_bot.storage import Storage

from helpers import settings


def seed(storage):
    # a well-calibrated model: 4 markets priced, 3 candidates, 2 winners
    rows = [("A-1-X", "0.80", "0.45", "bid", 1, "yes"), ("A-1-Y", "0.10", "0.30", "bid", 0, "no"),
            ("A-1-Z", "0.70", "0.40", "bid", 1, "no"), ("A-1-W", "0.20", "0.60", "ask", 1, "no")]
    for t, p, price, side, acc, res in rows:
        storage.log_decision("weather", "model", bool(acc), "test", market_ticker=t, model_prob=Decimal(p), price=Decimal(price), book_side=side, count=5)
        storage.save_market_result(t, "A-1", res, time.time())
    storage.log_decision("weather", "model", True, "unresolved", market_ticker="A-1-U", model_prob=Decimal("0.5"), price=Decimal("0.5"), book_side="bid", count=5)


def test_backtest_scores_and_caveats(tmp_path):
    s = Storage(tmp_path / "x.db")
    s.set_state("model_version", MODEL_VERSION), s.set_state("model_version_since", 0)   # seeded decisions belong to this model
    seed(s)
    rep = run_backtest(s, since_days=1)
    assert rep.n_scored == 4 and rep.unresolved == 1 and rep.n_candidates == 3
    assert rep.brier_model is not None and rep.brier_model_paired < rep.brier_market
    assert rep.n_paired == 4 and rep.n_contested == 4 and rep.verdict.startswith("insufficient evidence")
    # X: bid 0.45 -> fill 0.46, settles yes: +0.54*5 - fee ; W: ask 0.60 -> fill 0.59, settles no: +0.59*5 - fee ; Z: bid 0.40 -> fill 0.41 settles no: -0.41*5 - fee
    assert rep.pnl_cents > 0 and rep.hit_rate == 2 / 3
    assert any("Sample" in c for c in rep.caveats) and len(rep.calibration) >= 2
    rev = weekly_review(s, days=1)
    assert rev["proposals"] and rev["per_strategy"]["weather"]["decisions"] == 5


def test_backtest_verdict_needs_contested_sample_and_ignores_missing_market_prices(tmp_path):
    s = Storage(tmp_path / "x.db")
    s.set_state("model_version", MODEL_VERSION), s.set_state("model_version_since", 0)   # seeded decisions belong to this model
    # 25 far-from-the-money markets with no market price: the model is trivially right; the market must not score
    for i in range(25):
        s.log_decision("crypto", "model", False, "no two-sided book", market_ticker=f"C-{i}", model_prob=Decimal("0.0"))
        s.save_market_result(f"C-{i}", "C", "no", time.time())
    rep = run_backtest(s, since_days=1)
    assert rep.n_scored == 25 and rep.n_paired == 0 and rep.brier_market is None and rep.n_contested == 0
    assert rep.verdict.startswith("insufficient evidence") and "Only 0 contested" in rep.caveats[0]
    # 20 contested markets where the model is worse than the market -> a negative verdict
    for i in range(20):
        s.log_decision("crypto", "model", True, "t", market_ticker=f"B-{i}", model_prob=Decimal("0.9"), price=Decimal("0.2"), book_side="bid", count=1)
        s.save_market_result(f"B-{i}", "B", "no", time.time())
    rep = run_backtest(s, since_days=1)
    assert rep.n_contested == 20 and rep.verdict.startswith("model does NOT beat")
    assert rep.caveats[0].startswith("On contested markets the model's Brier score is NOT better")
    # and a positive one when the model is right where the market was wrong
    s2 = Storage(tmp_path / "y.db")
    s2.set_state("model_version", MODEL_VERSION), s2.set_state("model_version_since", 0)
    for i in range(20):
        s2.log_decision("weather", "model", True, "t", market_ticker=f"W-{i}", model_prob=Decimal("0.8"), price=Decimal("0.3"), book_side="bid", count=1)
        s2.save_market_result(f"W-{i}", "W", "yes", time.time())
    assert run_backtest(s2, since_days=1).verdict.startswith("model beats")


def test_dashboard_renders_and_halt_button_works(tmp_path):
    st = settings(tmp_path)
    s = Storage(st.db_path)
    s.log_decision("ladder_arb", "ladder", True, "net 7c", market_ticker="A-1-X", price=Decimal("0.4"), book_side="bid")
    s.set_state("day_key", "2026-09-10")
    html = render(status_payload(st, s))
    assert "HALT ALL ORDER PLACEMENT" in html and "net 7c" in html and "no HALT file" in html
    d = Dashboard(st, s, "127.0.0.1", 0)
    d.start()
    try:
        host, port = d.server.server_address[:2]
        c = http.client.HTTPConnection(host, port, timeout=5)
        c.request("GET", "/api/status")
        r = c.getresponse()
        assert r.status == 200 and json.loads(r.read())["halt_file"] is False
        c.request("POST", "/halt")
        r = c.getresponse()
        assert r.status == 303 and st.halt_path.exists()
        c.request("GET", "/")
        assert b"HALT file present" in c.getresponse().read()
        c.request("POST", "/resume-file")
        c.getresponse().read()
        assert not st.halt_path.exists()
    finally:
        d.stop()
        s.close()


def test_dashboard_renders_scorecard(tmp_path):
    from kalshi_bot.dashboard import render_scorecard

    assert "No settlements scored yet" in render_scorecard(None)
    good = {"ts": time.time(), "n_scored": 12, "n_contested": 12, "unresolved": 3, "n_candidates": 4, "pnl_cents": "35", "hit_rate": 0.75,
            "verdict": "model beats the market's prices on 12 contested settlements (Brier 0.1800 vs 0.2200, lower is better)",
            "calibration": [{"bucket": "0.6-0.7", "n": 5, "mean_p": 0.64, "realized": 0.6}], "caveats": ["Sample: small"]}
    html = render_scorecard(good)
    assert "model beats the market" in html and "0.6-0.7" in html and "Sample: small" in html and "class='ok'" in html
    bad = dict(good, verdict="model does NOT beat the market's prices on 12 contested settlements")
    assert "does NOT beat" in render_scorecard(bad) and "class='bad'" in render_scorecard(bad)
    st = settings(tmp_path)
    s = Storage(st.db_path)
    s.set_state("last_backtest", good)
    page = render(status_payload(st, s))
    assert "Model scorecard" in page and "model beats the market" in page and "last_backtest" not in page.split("Risk state")[1].split("Equity")[0]
    s.close()


def test_activity_stats_counts_distinct_positions_per_day(tmp_path):
    from kalshi_bot.backtest import activity_stats

    import calendar

    s = Storage(tmp_path / "x.db")

    s.set_state("model_version", MODEL_VERSION), s.set_state("model_version_since", 0)   # seeded decisions belong to this model
    assert activity_stats(s)["trades_per_week"] == 0.0
    now = calendar.timegm((2026, 9, 10, 12, 0, 0))  # fixed noon UTC so the hour never crosses a day boundary
    for i in range(6):  # the same two markets observed every scan for an hour count once each
        s.log_decision("weather", "execute", True, "OBSERVE mode: order not sent", market_ticker="A-1-X", ts=now - 3600 + i * 600)
        s.log_decision("weather", "execute", True, "OBSERVE mode: order not sent", market_ticker="A-1-Y", ts=now - 3600 + i * 600)
    s.log_decision("crypto", "execute", True, "filled: all legs filled", market_ticker="B-1", ts=now - 1800)
    s.log_decision("crypto", "risk", False, "risk rejected", market_ticker="B-2", ts=now - 1800)  # not an execution
    a = activity_stats(s, days=7, now=now)
    assert a["distinct_positions"] == 3 and a["by_strategy"] == {"weather": 2, "crypto": 1}
    assert a["days_observed"] >= 0.04 and a["trades_per_day"] > 0  # rounded to 2 decimals; one hour is 0.0417 days


def test_dashboard_bot_status_line(tmp_path):
    st = settings(tmp_path)
    s = Storage(st.db_path)
    page = render(status_payload(st, s))
    assert "no bot process is attached" in page
    bot = {"pid": 42, "process_started": time.time() - 100, "phase": "running", "scan_interval_sec": 30, "modes": {"weather": "observe"},
           "market_data_env": "live", "orders_env": "demo", "last_scan_ts": time.time() - 12, "error": None}
    page = render(status_payload(st, s, extra=lambda: {"bot": bot, "last_scan": "scan 1.0s: 2 strategies"}))
    assert "class='ok'>running" in page and "pid 42" in page and "scan 1.0s: 2 strategies" in page
    assert "market data from live, orders to demo" in page and "scans every 30s" in page
    starting = dict(bot, phase="starting: connecting to the exchange", last_scan_ts=None, modes=None, market_data_env=None)
    page = render(status_payload(st, s, extra=lambda: {"bot": starting}))
    assert "class='bad'>starting: connecting to the exchange" in page and "last scan" not in page
    failed = dict(bot, phase="startup failed", error="ApiError: 503 service unavailable", last_scan_ts=None)
    page = render(status_payload(st, s, extra=lambda: {"bot": failed}))
    assert "class='bad'>startup failed" in page and "ApiError: 503 service unavailable" in page
    s.close()


def test_dashboard_waits_for_a_busy_port_then_binds(tmp_path):
    import socket
    import threading

    st = settings(tmp_path)
    s = Storage(st.db_path)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        with pytest.raises(OSError):
            Dashboard(st, s, "127.0.0.1", port, bind_timeout=0)  # no patience: the busy port is an error
        threading.Timer(0.6, blocker.close).start()
        t0 = time.monotonic()
        d = Dashboard(st, s, "127.0.0.1", port, bind_timeout=10)  # the old process lets go: bind succeeds on retry
        assert d.server.server_address[1] == port and time.monotonic() - t0 >= 0.5
        d.server.server_close()
    finally:
        blocker.close()
        s.close()


def test_dashboard_render_bug_becomes_an_error_page(tmp_path, monkeypatch):
    from kalshi_bot import dashboard as dashboard_mod

    st = settings(tmp_path)
    s = Storage(st.db_path)

    def broken(payload):
        raise KeyError("mean_p")

    monkeypatch.setattr(dashboard_mod, "render", broken)
    d = Dashboard(st, s, "127.0.0.1", 0)
    d.start()
    try:
        host, port = d.server.server_address[:2]
        c = http.client.HTTPConnection(host, port, timeout=5)
        c.request("GET", "/")
        r = c.getresponse()
        body = r.read().decode()
        assert r.status == 500 and "dashboard error" in body and "KeyError" in body
        c.request("GET", "/api/status")  # the JSON endpoint does not go through render() and still works
        assert c.getresponse().status == 200
    finally:
        d.stop()
        s.close()


def test_scorecard_sees_settled_markets_behind_a_flood_of_newer_rows(tmp_path):
    """The old query read only the newest 100k rows, so a busy bot hid every settled market from the scorecard."""
    st = settings(tmp_path)
    s = Storage(st.db_path, log_heartbeat_sec=0)
    s.set_state("model_version", MODEL_VERSION), s.set_state("model_version_since", 0)   # the seeded decisions belong to this model
    t0 = time.time() - 5 * 86400
    s.log_decision("weather", "model", True, "settled long ago", market_ticker="OLD-1", model_prob=Decimal("0.80"), price=Decimal("0.45"),
                   book_side="bid", count=5, ts=t0)
    s.save_market_result("OLD-1", "OLD", "yes", t0 + 3600)
    s.conn.execute("BEGIN")
    for i in range(3000):
        s.log_decision("crypto", "model", False, "newer view", market_ticker=f"NEW-{i % 300}", model_prob=Decimal("0.5"), price=Decimal("0.5"),
                       ts=t0 + 86400 + i)
    s.conn.execute("COMMIT")
    rep = run_backtest(s, since_days=30)
    assert rep.n_scored == 1 and rep.unresolved == 300
    s.close()


def test_dashboard_shows_a_failed_scoring_run(tmp_path):
    from kalshi_bot.dashboard import render_scorecard

    st = settings(tmp_path)
    s = Storage(st.db_path)
    s.set_state("last_scoring_error", {"ts": time.time(), "error": "ValueError: bad row"})
    page = render(status_payload(st, s))
    assert "Scoring failed at" in page and "ValueError: bad row" in page and "last_scoring_error" not in page.split("Risk state")[1].split("Equity")[0]
    assert "Scoring failed" not in render_scorecard(None, None)
    s.close()


def test_backtest_breaks_scores_down_by_strategy(tmp_path):
    from kalshi_bot.dashboard import render_scorecard

    st = settings(tmp_path)
    s = Storage(st.db_path, log_heartbeat_sec=0)
    s.set_state("model_version", MODEL_VERSION), s.set_state("model_version_since", 0)   # the seeded decisions belong to this model
    t = time.time() - 3600
    # weather: two contested markets, model far off; crypto: two contested markets, model close
    rows = [("weather", "W-1", "0.80", "0.30", 0), ("weather", "W-2", "0.20", "0.70", 1),
            ("crypto", "C-1", "0.30", "0.40", 0), ("crypto", "C-2", "0.70", "0.55", 1), ("crypto", "C-3", "0.99", "0.99", 1)]
    for strat, tk, p, price, res in rows:
        s.log_decision(strat, "model", False, "view", market_ticker=tk, model_prob=Decimal(p), price=Decimal(price), book_side="bid", ts=t)
        s.save_market_result(tk, tk.split("-")[0], "yes" if res else "no", t + 60)
    s.log_decision("crypto", "model", True, "candidate", market_ticker="C-2", model_prob=Decimal("0.70"), price=Decimal("0.55"), book_side="bid",
                   count=5, ts=t + 1)
    rep = run_backtest(s, since_days=1)
    by = rep.to_dict()["by_strategy"]
    assert by["weather"]["n_scored"] == 2 and by["weather"]["n_contested"] == 2
    assert by["crypto"]["n_scored"] == 3 and by["crypto"]["n_contested"] == 2   # the 0.99 market is not contested
    assert by["weather"]["brier_model_contested"] > by["weather"]["brier_market_contested"]
    assert by["crypto"]["brier_model_contested"] < by["crypto"]["brier_market_contested"]
    assert by["crypto"]["n_candidates"] == 1 and by["crypto"]["hits"] == 1 and Decimal(by["crypto"]["pnl_cents"]) > 0
    # at the moment of the trade the model said 0.70 against a 0.55 price and YES happened: the model knew better
    assert abs(rep.brier_model_at_trade - 0.09) < 1e-9 and abs(rep.brier_market_at_trade - 0.2025) < 1e-9
    assert "model knew better" in render_scorecard(rep.to_dict())
    assert "n_candidates" not in by["weather"]
    page = render_scorecard(rep.to_dict())
    assert "By strategy" in page and "<td>weather</td>" in page and "<td>crypto</td>" in page
    s.close()


def test_scorecard_starts_over_at_a_model_change(tmp_path):
    """Decisions made by an earlier model version are not scored (unless asked for), so a rewrite is judged on its own."""
    from kalshi_bot.backtest import MODEL_VERSION, model_version_since

    st = settings(tmp_path)
    s = Storage(st.db_path, log_heartbeat_sec=0)
    t_old = time.time() - 3 * 86400
    s.log_decision("weather", "model", True, "old model", market_ticker="OLD-1", model_prob=Decimal("0.80"), price=Decimal("0.45"), book_side="bid",
                   count=5, ts=t_old)
    s.save_market_result("OLD-1", "OLD", "no", t_old + 3600)
    s.set_state("model_version", "previous")
    s.set_state("model_version_since", t_old - 86400)
    marker = model_version_since(s, now=t_old + 86400)   # the new version first runs a day after the old decision
    assert s.all_state()["model_version"] == MODEL_VERSION and marker == t_old + 86400
    assert run_backtest(s, since_days=30).n_scored == 0                             # the old decision is not this model's
    assert run_backtest(s, since_days=30, since_model_change=False).n_scored == 1   # --all still shows it
    s.log_decision("weather", "model", True, "new model", market_ticker="NEW-1", model_prob=Decimal("0.70"), price=Decimal("0.40"), book_side="bid",
                   count=5, ts=t_old + 2 * 86400)
    s.save_market_result("NEW-1", "NEW", "yes", t_old + 2 * 86400 + 60)
    rep = run_backtest(s, since_days=30)
    assert rep.n_scored == 1 and rep.to_dict()["model_version"] == MODEL_VERSION
    assert "Only decisions made by this version" in render(status_payload(st, s)) or True  # rendered once the card is stored
    s.set_state("last_backtest", rep.to_dict())
    assert "Only decisions made by this version" in render(status_payload(st, s))
    s.close()


def test_gap_summary_answers_whether_arbitrage_found_anything(tmp_path):
    from kalshi_bot.backtest import gap_summary

    s = Storage(tmp_path / "g.db")
    now = time.time()
    assert "nothing to trade" in gap_summary(s, days=7, now=now)["verdict"]

    # three gaps, none clearing 5c after fees: the honest answer is "nothing tradeable"
    for i, net in enumerate(["-2.2", "1.4", "4.9"]):
        s.log_arb_gap(f"E-{i}", "sum_yes_asks_below_1", 4, "0.98", "6.0", "4.6", net, 5, [], ts=now - 3600 * (i + 1))
    out = gap_summary(s, days=7, now=now)
    assert out["gaps_logged"] == 3 and out["cleared_threshold"] == 0
    assert out["best_net_cents"] == "4.9" and "none cleared 5c" in out["verdict"]
    assert out["by_kind"]["sum_yes_asks_below_1"] == {"logged": 3, "cleared": 0}

    # one that does clear, and the verdict changes to something actionable
    s.log_arb_gap("E-9", "crossed_book", 2, "0.97", "9.0", "2.0", "7.0", 5, [], ts=now - 600)
    out = gap_summary(s, days=7, now=now)
    assert out["cleared_threshold"] == 1 and out["events_with_a_clearing_gap"] == ["E-9"]
    assert "1 of 4 logged gaps cleared 5c" in out["verdict"] and "structural, not forecasts" in out["verdict"]
    s.close()
