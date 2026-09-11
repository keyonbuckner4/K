import json
import shutil
from pathlib import Path

from kalshi_bot import cli
from kalshi_bot.storage import Storage

REPO = Path(__file__).resolve().parents[1]


def make_root(tmp_path):
    (tmp_path / "BRIEF.md").write_text("spec")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "config").mkdir()
    shutil.copy(REPO / "config" / "bot.toml", tmp_path / "config" / "bot.toml")
    return tmp_path


def test_halt_resume_status_offline(tmp_path, capsys):
    root = make_root(tmp_path)
    assert cli.main(["--root", str(root), "halt", "testing"]) == 0
    assert (root / "HALT").read_text().strip() == "testing"
    capsys.readouterr()
    assert cli.main(["--root", str(root), "status", "--offline"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["halt_file"] is True and out["env"] == "demo"
    assert cli.main(["--root", str(root), "resume", "--file"]) == 0
    assert not (root / "HALT").exists()


def test_live_requires_confirm(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    monkeypatch.delenv("CONFIRM_LIVE", raising=False)
    assert cli.main(["--root", str(root), "--live", "status", "--offline"]) == 2


def test_gaps_and_decisions_read_db(tmp_path, capsys):
    root = make_root(tmp_path)
    db = root / "data" / "bot.demo.db"
    s = Storage(db)
    s.log_decision("ladder_arb", "ladder", False, "net 2c < 5c", event_ticker="E")
    s.log_arb_gap("E", "sum_yes_asks_below_1", 4, "0.98", "2", "4.2", "-2.2", 5, [])
    s.close()
    assert cli.main(["--root", str(root), "gaps"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["kind"] == "sum_yes_asks_below_1"
    assert cli.main(["--root", str(root), "decisions"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["reason"] == "net 2c < 5c"


def test_balance_without_credentials_fails_cleanly(tmp_path, capsys):
    root = make_root(tmp_path)
    assert cli.main(["--root", str(root), "--quiet", "balance"]) == 1
    assert "KALSHI_API_KEY_ID" in capsys.readouterr().err


def test_parser_has_every_command():
    p = cli.build_parser()
    names = set(cli.COMMANDS) | set(cli.SYNC_COMMANDS)
    assert {"balance", "doctor", "scan", "run", "halt", "resume", "backtest", "review", "dashboard", "watch", "flatten"} <= names
    args = p.parse_args(["run", "--trade", "--strategy", "ladder_arb", "--dashboard"])
    assert args.trade and args.strategy == ["ladder_arb"] and args.dashboard


def test_reason_histogram_groups_numbers():
    from kalshi_bot.engine import reason_histogram

    rows = [{"strategy": "crypto", "stage": "model", "reason": "no two-sided book"},
            {"strategy": "crypto", "stage": "model", "reason": "no two-sided book"},
            {"strategy": "weather", "stage": "model", "reason": "best side bid: net edge -3.20c < 5c"},
            {"strategy": "weather", "stage": "model", "reason": "best side ask: net edge 1.05c < 5c"}]
    h = reason_histogram(rows)
    assert h[0] == ("crypto/model: no two-sided book", 2)
    assert ("weather/model: best side bid: net edge #c < #c", 1) in h
