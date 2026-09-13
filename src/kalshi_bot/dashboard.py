"""Minimal status page with a HALT button. Standard library only, binds to localhost."""

from __future__ import annotations

import errno
import html
import json
import logging
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from . import halt as halt_mod
from .config import Settings
from .storage import Storage

log = logging.getLogger(__name__)


def status_payload(settings: Settings, storage: Storage, extra: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    state = storage.all_state()
    eq = storage.equity_history(limit=1)
    payload = {
        "env": settings.env, "time": time.time(), "halt_file": halt_mod.halt_active(settings.halt_path),
        "risk_state": state, "equity": eq[0] if eq else None,
        "recent_decisions": storage.decisions(limit=25), "recent_gaps": storage.arb_gaps(limit=10),
        "open_intents": storage.intents(limit=10),
    }
    if extra:
        try:
            payload.update(extra())
        except Exception as e:  # never let the page crash the bot
            payload["extra_error"] = str(e)
    return payload


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="10"><title>kalshi-bot {env}</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;background:#111;color:#eee}} .halt{{background:#c00;color:#fff;font-size:20px;padding:12px 24px;border:0;border-radius:6px;cursor:pointer}}
.ok{{color:#5f5}} .bad{{color:#f55}} table{{border-collapse:collapse;font-size:13px}} td,th{{border:1px solid #333;padding:4px 8px;text-align:left}} pre{{background:#222;padding:8px;overflow:auto}}</style></head>
<body><h1>kalshi-bot <span class="{cls}">{env}</span></h1>
<form method="post" action="/halt"><button class="halt" type="submit">HALT ALL ORDER PLACEMENT</button></form>
{halt_line}
{bot_line}
<h2>Model scorecard</h2>{scorecard}
<h2>Risk state</h2><pre>{state}</pre>
<h2>Equity</h2><pre>{equity}</pre>
<h2>Recent decisions</h2><table><tr><th>time</th><th>strategy</th><th>stage</th><th>ok</th><th>market</th><th>side</th><th>price</th><th>edge net</th><th>reason</th></tr>{decisions}</table>
<h2>Recent ladder gaps</h2><table><tr><th>time</th><th>event</th><th>kind</th><th>legs</th><th>gross</th><th>fees</th><th>net</th></tr>{gaps}</table>
</body></html>"""


def _fmt_ts(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "?"


def render_scorecard(card: dict[str, Any] | None, error: dict[str, Any] | None = None) -> str:
    warn = ""
    if error:
        warn = (f"<p class='bad'>Scoring failed at {_fmt_ts(error.get('ts'))}: {html.escape(str(error.get('error')))}. "
                f"The numbers below are from the last successful run; the traceback is in the log file.</p>")
    if not card:
        return warn + "<p>No settlements scored yet. The bot re-scores every hour; the first weather settlements arrive the morning after a market's date.</p>"
    verdict = str(card.get("verdict") or "no scored markets yet")
    good = verdict.startswith("model beats")
    cal = "".join(f"<tr><td>{c['bucket']}</td><td>{c['n']}</td><td>{c['mean_p']:.2f}</td><td>{c['realized']:.2f}</td></tr>" for c in card.get("calibration", []))
    caveats = "".join(f"<li>{html.escape(str(c))}</li>" for c in card.get("caveats", []))
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(card.get("ts", 0)))) if card.get("ts") else "?"
    act = card.get("activity") or {}
    activity = ""
    if act:
        by = ", ".join(f"{k}: {v}" for k, v in (act.get("by_strategy") or {}).items()) or "none"
        activity = (f"<p>Trade rate: {act.get('distinct_positions', 0)} distinct positions would have traded over {act.get('days_observed', 0)} days, "
                    f"about {act.get('trades_per_day', 0)} per day or {act.get('trades_per_week', 0)} per week ({by}).</p>")
    return (warn + activity + f"<p>Last scored {when}. Scored markets: {card.get('n_scored')} (contested: {card.get('n_contested', 0)}), unresolved: {card.get('unresolved')}, "
            f"candidates: {card.get('n_candidates')}, pessimistic P&amp;L: {card.get('pnl_cents')}c, hit rate: {card.get('hit_rate')}.</p>"
            f"<p><b class='{'ok' if good else 'bad'}'>{html.escape(verdict)}</b></p>"
            f"<table><tr><th>model prob</th><th>n</th><th>mean p</th><th>realized</th></tr>{cal}</table>"
            f"<p>What would make this wrong:</p><ul>{caveats}</ul>")


def render_bot_line(payload: dict[str, Any]) -> str:
    """One line on the bot process itself: phase, age, last scan. Absent for `bot dashboard` (no engine)."""
    bot = payload.get("bot")
    if not bot:
        return "<p>Status page only: no bot process is attached (started with `bot dashboard`, or the run has not published its status).</p>"
    phase = str(bot.get("phase", "?"))
    cls = "ok" if phase == "running" else "bad"
    parts = [f"<b class='{cls}'>{html.escape(phase)}</b>", f"pid {bot.get('pid')}", f"process started {_fmt_ts(bot.get('process_started'))}"]
    if bot.get("error"):
        parts.append(f"error: {html.escape(str(bot['error']))}")
    if bot.get("modes"):
        parts.append("modes " + html.escape(json.dumps(bot["modes"])))
    if bot.get("market_data_env"):
        parts.append(f"market data from {html.escape(str(bot['market_data_env']))}, orders to {html.escape(str(bot.get('orders_env')))}")
    last = bot.get("last_scan_ts")
    if last:
        age = max(0.0, float(payload.get("time", time.time())) - float(last))
        parts.append(f"last scan {_fmt_ts(last)} ({age:.0f}s ago; scans every {float(bot.get('scan_interval_sec', 0)):.0f}s)")
        if payload.get("last_scan"):
            parts.append(html.escape(str(payload["last_scan"])))
    elif phase == "running":
        parts.append("first scan in progress")
    return "<p>Bot: " + " | ".join(parts) + "</p>"


def render(payload: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><td>{time.strftime('%H:%M:%S', time.localtime(d['ts']))}</td><td>{html.escape(d['strategy'])}</td><td>{d['stage']}</td>"
        f"<td class='{'ok' if d['accepted'] else 'bad'}'>{'yes' if d['accepted'] else 'no'}</td><td>{html.escape(str(d['market_ticker'] or ''))}</td>"
        f"<td>{d['book_side'] or ''}</td><td>{d['price'] or ''}</td><td>{d['edge_net_cents'] or ''}</td><td>{html.escape(str(d['reason']))}</td></tr>"
        for d in payload["recent_decisions"])
    gaps = "".join(f"<tr><td>{time.strftime('%H:%M:%S', time.localtime(g['ts']))}</td><td>{html.escape(g['event_ticker'])}</td><td>{g['kind']}</td>"
                   f"<td>{g['n_legs']}</td><td>{g['gross_edge_cents']}</td><td>{g['fee_cents']}</td><td>{g['net_edge_cents']}</td></tr>" for g in payload["recent_gaps"])
    halted = payload["halt_file"]
    halt_line = "<p class='bad'>HALT file present: all order placement blocked. <form method='post' action='/resume-file' style='display:inline'><button type='submit'>remove HALT file</button></form></p>" if halted else "<p class='ok'>no HALT file</p>"
    state = {k: v for k, v in payload["risk_state"].items() if k not in ("last_backtest", "last_scoring_error")}
    return PAGE.format(env=payload["env"], cls="bad" if payload["env"] == "live" else "ok", halt_line=halt_line, bot_line=render_bot_line(payload),
                       scorecard=render_scorecard(payload["risk_state"].get("last_backtest"), payload["risk_state"].get("last_scoring_error")),
                       state=html.escape(json.dumps(state, indent=1, default=str)), equity=html.escape(json.dumps(payload["equity"], default=str)),
                       decisions=rows, gaps=gaps)


def make_handler(settings: Settings, storage: Storage, extra: Callable[[], dict[str, Any]] | None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            log.debug(fmt, *args)

        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            # A rendering bug must show up as an error page, never as a dropped connection.
            try:
                payload = status_payload(settings, storage, extra)
                if self.path.startswith("/api/status"):
                    self._send(200, json.dumps(payload, default=str), "application/json")
                else:
                    self._send(200, render(payload))
            except Exception:
                log.exception("dashboard request %s failed", self.path)
                self._send(500, f"<pre>dashboard error (the bot itself keeps running):\n{html.escape(traceback.format_exc())}</pre>")

        def do_POST(self):
            if self.path == "/halt":
                halt_mod.engage(settings.halt_path, "dashboard")
                log.critical("HALT engaged from dashboard")
            elif self.path == "/resume-file":
                halt_mod.release(settings.halt_path)
                log.warning("HALT file removed from dashboard")
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

    return Handler


class Dashboard:
    def __init__(self, settings: Settings, storage: Storage, host: str = "127.0.0.1", port: int = 8787, extra: Callable[[], dict[str, Any]] | None = None,
                 bind_timeout: float = 20.0):
        handler = make_handler(settings, storage, extra)
        deadline = time.monotonic() + bind_timeout
        while True:
            try:
                self.server = ThreadingHTTPServer((host, port), handler)
                break
            except OSError as e:
                # A restart can race the previous process releasing the port; wait for it rather than crashing.
                if e.errno not in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048)) or time.monotonic() >= deadline:
                    raise
                log.warning("dashboard port %s:%s busy (%s); retrying", host, port, e)
                time.sleep(1.0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        h, p = self.server.server_address[:2]
        return f"http://{h}:{p}/"

    def start(self) -> None:
        self.thread.start()
        log.info("dashboard at %s", self.url)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
