"""The portable status snapshot: what it says, and the distinction it must never blur."""

import json
import time
from decimal import Decimal

from kalshi_bot import cli, halt
from kalshi_bot.status import build_status, render_markdown, write_report
from kalshi_bot.storage import Storage

from helpers import settings


def card(**over):
    base = {"ts": time.time(), "model_version": "v-test", "verdict": "model does NOT beat the market's prices on 93 contested settlements",
            "n_scored": 3044, "n_contested": 93, "unresolved": 840, "n_candidates": 32, "hit_rate": 0.09375,
            "pnl_cents": "-463.0000", "brier_model_contested": 0.1216, "brier_market_contested": 0.0917,
            "brier_model_at_trade": 0.31, "brier_market_at_trade": 0.22,
            "by_strategy": {"weather": {"n_scored": 2000, "n_contested": 60, "brier_model_contested": 0.14,
                                        "brier_market_contested": 0.09, "n_candidates": 30, "hits": 2}},
            "activity": {"trades_per_day": 24.0, "trades_per_week": 168.0, "days_observed": 2.04}}
    base.update(over)
    return base


def test_report_separates_real_trades_from_simulated_ones(tmp_path):
    """The distinction the operator actually asked about: no order has ever been sent, so there is
    no real win/loss record, only a simulated one. The report must say that in words."""
    st = settings(tmp_path)
    s = Storage(st.db_path)
    s.set_state("last_backtest", card())
    status = build_status(st, s, now=1_789_500_000.0)

    assert status["orders_placed_ever"] == 0 and status["fills_ever"] == 0
    assert status["scorecard"]["would_be_trades"] == 32 and status["scorecard"]["hit_rate"] == 0.09375
    md = render_markdown(status)
    assert "No order has ever been placed" in md and "no real win/loss record yet" in md
    assert "| Win rate | 9% |" in md and "| Trades it would have made | 32 |" in md
    assert "-463.0000" in md and "does NOT beat" in md
    assert "| weather | 2000 | 60 | 0.14 | 0.09 | 30 | 2 |" in md
    assert "24.0 per day, 168.0 per week" in md
    s.close()


def test_report_reports_real_orders_once_there_are_any(tmp_path):
    st = settings(tmp_path)
    s = Storage(st.db_path)
    s.set_state("last_backtest", card())
    s.save_intent("i1", "weather", "E", "filled", "trade", Decimal("6"), 225, [])
    s.save_order("o1", "coid-1", "i1", "KXHIGHNY-1", "bid", Decimal("0.45"), 5, "immediate_or_cancel", "filled")
    md = render_markdown(build_status(st, s))
    assert "1 orders placed" in md and "No order has ever been placed" not in md
    s.close()


def test_account_figures_are_left_out_unless_asked_for(tmp_path):
    """The repository is public, so equity is opt-in rather than default."""
    st = settings(tmp_path)
    s = Storage(st.db_path)
    s.snapshot_equity(20_000, 0, 0)
    assert "equity" not in build_status(st, s)
    assert build_status(st, s, include_equity=True)["equity"] is not None
    s.close()


def test_report_surfaces_halts_and_scoring_failures(tmp_path):
    st = settings(tmp_path)
    s = Storage(st.db_path)
    s.set_state("full_stop", True)
    s.set_state("full_stop_reason", "drawdown 12% from peak")
    s.set_state("last_scoring_error", {"ts": time.time(), "error": "ValueError: bad row"})
    halt.engage(st.halt_path, "manual")
    md = render_markdown(build_status(st, s))
    assert "HALT file present" in md and "full_stop = True" in md and "ValueError: bad row" in md
    s.close()


def test_empty_database_renders_without_pretending_to_know_anything(tmp_path):
    st = settings(tmp_path)
    s = Storage(st.db_path)
    md = render_markdown(build_status(st, s))
    assert "nothing scored yet" in md and "| Win rate | n/a |" in md
    assert "Last scored: never" in md
    s.close()


def test_report_command_writes_both_files(tmp_path, capsys):
    root = tmp_path
    (root / "BRIEF.md").write_text("spec")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "config").mkdir()
    (root / "config" / "bot.toml").write_text(
        '[hosts.demo]\nrest = "https://demo.test/trade-api/v2"\nws = "wss://demo.test/trade-api/ws/v2"\n'
        '[storage]\ndb_path = "data/bot.db"\n')
    s = Storage(root / "data" / "bot.demo.db")
    s.set_state("last_backtest", card())
    s.close()

    assert cli.main(["--root", str(root), "report"]) == 0
    assert "| Win rate | 9% |" in capsys.readouterr().out
    md = root / "data" / "status" / "latest.md"
    js = root / "data" / "status" / "latest.json"
    assert md.exists() and js.exists()
    assert json.loads(js.read_text())["scorecard"]["would_be_trades"] == 32
    assert "No order has ever been placed" in md.read_text()


def test_write_report_returns_paths(tmp_path):
    st = settings(tmp_path)
    s = Storage(st.db_path)
    j, m = write_report(tmp_path, build_status(st, s))
    assert j.name == "latest.json" and m.name == "latest.md" and j.parent.name == "status"
    s.close()
