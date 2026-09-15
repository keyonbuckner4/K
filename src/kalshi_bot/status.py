"""A portable status snapshot: the numbers the dashboard shows, as a file.

The dashboard binds to localhost, so everything the bot knows is invisible the moment you are away
from the machine it runs on. That has blocked every check-in so far: answering "what is my win rate"
needed somebody physically at the PC. This renders the same numbers into JSON and Markdown that can
be mailed, pasted, or served through whatever channel the operator chooses.

Deliberately no publishing here. Where this snapshot goes is an operator decision, not a default:
the repository is public, so the account figures are omitted unless ``include_equity`` asks for them,
and even the model numbers are only worth publishing somewhere the operator picked on purpose.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from . import halt as halt_mod
from .config import Settings
from .storage import Storage

log = logging.getLogger(__name__)


def build_status(settings: Settings, storage: Storage, extra: dict[str, Any] | None = None,
                 include_equity: bool = False, now: float | None = None) -> dict[str, Any]:
    """Everything worth knowing about the run, from the database alone (no network)."""
    now = time.time() if now is None else now
    state = storage.all_state()
    card = state.get("last_backtest") or {}
    activity = card.get("activity") or {}
    out: dict[str, Any] = {
        "generated_utc": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(now)),
        "env": settings.env,
        "halt_file": halt_mod.halt_active(settings.halt_path),
        "halts": {k: state.get(k) for k in ("daily_halt_day", "weekly_halt", "full_stop", "full_stop_reason") if state.get(k)},
        "model_version": card.get("model_version"),
        "scored_at_utc": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(float(card["ts"]))) if card.get("ts") else None,
        # the distinction that matters most: what actually happened vs what would have happened
        "orders_placed_ever": storage.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
        "fills_ever": storage.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0],
        "scorecard": {
            "verdict": card.get("verdict"),
            "markets_scored": card.get("n_scored"),
            "contested": card.get("n_contested"),
            "unresolved": card.get("unresolved"),
            "would_be_trades": card.get("n_candidates"),
            "hit_rate": card.get("hit_rate"),
            "pnl_cents_pessimistic": card.get("pnl_cents"),
            "brier_model_contested": card.get("brier_model_contested"),
            "brier_market_contested": card.get("brier_market_contested"),
            "brier_model_at_trade": card.get("brier_model_at_trade"),
            "brier_market_at_trade": card.get("brier_market_at_trade"),
            "by_strategy": card.get("by_strategy") or {},
        },
        "activity": {"trades_per_day": activity.get("trades_per_day"), "trades_per_week": activity.get("trades_per_week"),
                     "days_observed": activity.get("days_observed")},
        "scoring_error": state.get("last_scoring_error"),
    }
    if include_equity:
        eq = storage.equity_history(limit=1)
        out["equity"] = eq[0] if eq else None
    if extra:
        out.update(extra)
    return out


def _pct(v: Any) -> str:
    try:
        return f"{float(v) * 100:.0f}%"
    except (TypeError, ValueError):
        return "n/a"


def _or_na(v: Any) -> str:
    return "n/a" if v is None else str(v)


def render_markdown(s: dict[str, Any]) -> str:
    sc = s.get("scorecard") or {}
    out = [f"# kalshi-bot status ({s.get('env')})", "",
           f"Generated {s.get('generated_utc')}. Model version: {s.get('model_version') or 'nothing scored yet'}.",
           f"Last scored: {s.get('scored_at_utc') or 'never'}.", ""]

    placed = int(s.get("orders_placed_ever") or 0)
    if placed == 0:
        out += ["**No order has ever been placed.** The bot is observing: it prices markets and records what it",
                "would have done, but sends nothing. There is no real win/loss record yet, and no money at risk.", ""]
    else:
        out += [f"**{placed} orders placed, {s.get('fills_ever')} fills.**", ""]

    out += ["## Would-be trades (simulated)", "",
            "| Measure | Value |", "|---|---|",
            f"| Trades it would have made | {_or_na(sc.get('would_be_trades'))} |",
            f"| Win rate | {_pct(sc.get('hit_rate'))} |",
            f"| Simulated P&L (cents) | {_or_na(sc.get('pnl_cents_pessimistic'))} |",
            f"| Markets scored | {_or_na(sc.get('markets_scored'))} |",
            f"| Contested (the ones that count) | {_or_na(sc.get('contested'))} |",
            "", f"**Verdict:** {sc.get('verdict') or 'nothing scored yet'}", ""]

    by = sc.get("by_strategy") or {}
    if by:
        out += ["## By strategy", "",
                "| Strategy | Scored | Contested | Brier model | Brier market | Trades | Winners |",
                "|---|---|---|---|---|---|---|"]
        for name, v in sorted(by.items()):
            out.append(f"| {name} | {v.get('n_scored', 0)} | {v.get('n_contested', 0)} | "
                       f"{_or_na(v.get('brier_model_contested'))} | {_or_na(v.get('brier_market_contested'))} | "
                       f"{v.get('n_candidates', 0)} | {v.get('hits', 0)} |")
        out.append("")

    act = s.get("activity") or {}
    if act.get("trades_per_week") is not None:
        out += [f"Trade rate: about {act.get('trades_per_day')} per day, {act.get('trades_per_week')} per week, "
                f"over {act.get('days_observed')} days observed.", ""]
    if s.get("halt_file"):
        out += ["**HALT file present: all order placement is blocked.**", ""]
    for k, v in (s.get("halts") or {}).items():
        out.append(f"- Halt active: {k} = {v}")
    if s.get("scoring_error"):
        out += ["", f"**Scoring failed:** {s['scoring_error'].get('error')}"]
    return "\n".join(out).rstrip() + "\n"


def write_report(root: Path, status: dict[str, Any]) -> tuple[Path, Path]:
    """Write the snapshot under ``data/status/``. Returns (json path, markdown path)."""
    d = root / "data" / "status"
    d.mkdir(parents=True, exist_ok=True)
    j, m = d / "latest.json", d / "latest.md"
    j.write_text(json.dumps(status, indent=2, default=str), encoding="utf-8")
    m.write_text(render_markdown(status), encoding="utf-8")
    return j, m
