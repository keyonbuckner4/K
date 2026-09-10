import http.client
import json
import time
from decimal import Decimal

from kalshi_bot.backtest import run_backtest
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
    seed(s)
    rep = run_backtest(s, since_days=1)
    assert rep.n_scored == 4 and rep.unresolved == 1 and rep.n_candidates == 3
    assert rep.brier_model is not None and rep.brier_model < rep.brier_market
    # X: bid 0.45 -> fill 0.46, settles yes: +0.54*5 - fee ; W: ask 0.60 -> fill 0.59, settles no: +0.59*5 - fee ; Z: bid 0.40 -> fill 0.41 settles no: -0.41*5 - fee
    assert rep.pnl_cents > 0 and rep.hit_rate == 2 / 3
    assert any("Sample" in c for c in rep.caveats) and len(rep.calibration) >= 2
    rev = weekly_review(s, days=1)
    assert rev["proposals"] and rev["per_strategy"]["weather"]["decisions"] == 5


def test_backtest_flags_model_worse_than_market(tmp_path):
    s = Storage(tmp_path / "x.db")
    s.log_decision("crypto", "model", True, "t", market_ticker="B-1", model_prob=Decimal("0.9"), price=Decimal("0.2"), book_side="bid", count=1)
    s.save_market_result("B-1", "B", "no", time.time())
    rep = run_backtest(s, since_days=1)
    assert rep.caveats[0].startswith("The model's Brier score is NOT better")


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
