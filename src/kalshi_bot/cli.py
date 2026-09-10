"""``bot`` command line. Every module runs standalone from here; no framework.

Safety defaults: demo environment; observe-only; ``--live`` needs CONFIRM_LIVE=yes; ``run --trade``
is the only way orders are sent, and only for strategies whose config mode is ``trade``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import __version__, halt as halt_mod
from .backtest import run_backtest, sync_results
from .config import Settings, load_settings
from .errors import BotError, ConfigError, UnexpectedApiResponse
from .logging_setup import setup_logging
from .storage import Storage

log = logging.getLogger("kalshi_bot.cli")


def _settings(args: argparse.Namespace) -> Settings:
    return load_settings(Path(args.root) if args.root else None, live=bool(args.live))


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _engine(args: argparse.Namespace, settings: Settings, trade: bool = False, use_ws: bool = False):
    from .engine import Engine

    return Engine(settings, trade=trade, strategies=getattr(args, "strategy", None), use_ws=use_ws)


def _banner(settings: Settings) -> None:
    tag = "LIVE  (real money)" if settings.is_live else "demo"
    key = f"{settings.api_key_id[:6]}… from {settings.key_source}" if settings.has_credentials else "none"
    print(f"kalshi-bot {__version__} | env={tag} | rest={settings.rest_base_url} | key={key} | db={settings.db_path}", file=sys.stderr)


# ---- commands ------------------------------------------------------------------------------------
async def cmd_balance(args, settings):
    settings.require_credentials()
    eng = _engine(args, settings)
    try:
        bal = await eng.client.balance()
        _print({"env": settings.env, "balance_dollars": f"{bal.balance_cents / 100:.2f}", "portfolio_value_dollars": f"{bal.portfolio_value_cents / 100:.2f}",
                "equity_dollars": f"{bal.equity_cents / 100:.2f}", "updated": bal.updated})
    finally:
        await eng.close()


async def cmd_doctor(args, settings):
    eng = _engine(args, settings)
    try:
        _print(await eng.doctor())
    finally:
        await eng.close()


async def cmd_status(args, settings):
    storage = Storage(settings.db_path)
    out: dict[str, Any] = {"env": settings.env, "halt_file": halt_mod.halt_active(settings.halt_path), "risk_state": storage.all_state(),
                           "last_equity": (storage.equity_history(1) or [None])[0], "open_intents": storage.intents("filled", 20),
                           "recent_decisions": storage.decisions(limit=int(args.limit))}
    storage.close()
    if settings.has_credentials and not args.offline:
        eng = _engine(args, settings)
        try:
            snap = await eng.account.refresh(force=True)
            out["account"] = {"balance_cents": snap.balance_cents, "equity_cents": snap.equity_cents, "realized_cents": snap.realized_cents,
                              "positions": [{"ticker": p.ticker, "position": str(p.position), "exposure": str(p.market_exposure)} for p in snap.open_positions()],
                              "resting_orders": len(snap.resting_orders), "trading_active": snap.exchange.trading_active if snap.exchange else None}
            out["health"] = eng.risk.refresh(snap).__dict__
        finally:
            await eng.close()
    _print(out)


async def cmd_markets(args, settings):
    eng = _engine(args, settings)
    try:
        ms = await eng.client.markets(series_ticker=args.series, event_ticker=args.event, status=args.status, limit=min(int(args.limit), 1000), max_pages=1)
        rows = [{"ticker": m.ticker, "event": m.event_ticker, "status": m.status, "yes_bid": str(m.yes_bid), "yes_ask": str(m.yes_ask),
                 "close": m.close_time, "strike": f"{m.strike_type} {m.floor_strike} {m.cap_strike}", "title": (m.title or m.yes_sub_title or "")[:60]} for m in ms[: int(args.limit)]]
        _print(rows)
    finally:
        await eng.close()


async def cmd_events(args, settings):
    eng = _engine(args, settings)
    try:
        evs = await eng.client.events(series_ticker=args.series, status="open", with_nested_markets=True, max_pages=1)
        _print([{"event": e.event_ticker, "series": e.series_ticker, "mutually_exclusive": e.mutually_exclusive, "category": e.category,
                 "markets": len(e.markets), "strike_date": e.strike_date, "title": e.title} for e in evs])
    finally:
        await eng.close()


async def cmd_book(args, settings):
    eng = _engine(args, settings)
    try:
        for t in args.ticker:
            b = await eng.client.orderbook(t)
            _print(b.to_dict(depth=int(args.depth)))
    finally:
        await eng.close()


async def cmd_series(args, settings):
    eng = _engine(args, settings)
    try:
        s = await eng.client.series(args.ticker)
        _print({"ticker": s.ticker, "title": s.title, "category": s.category, "frequency": s.frequency, "fee_type": s.fee_type,
                "fee_multiplier": str(s.fee_multiplier), "settlement_sources": list(s.settlement_sources)})
    finally:
        await eng.close()


async def cmd_scan(args, settings):
    eng = _engine(args, settings, trade=False)
    try:
        info = await eng.startup()
        print("strategies:", info["strategies"], file=sys.stderr)
        rep = await eng.run(once=True)
        print(rep.summary())
        if args.verbose:
            _print([d for d in eng.storage.decisions(since=rep.started, limit=500)][::-1])
    finally:
        await eng.close()


async def cmd_run(args, settings):
    if args.trade:
        settings.require_credentials()
    eng = _engine(args, settings, trade=bool(args.trade), use_ws=not args.no_ws)
    if args.interval:
        eng.scan_interval = float(args.interval)
    dash = None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, eng.stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        info = await eng.startup()
        modes = {s.name: ("TRADE" if eng.can_trade and s.mode == "trade" else "observe") for s in eng.strategies}
        print(f"running every {eng.scan_interval:.0f}s; modes: {modes}; Ctrl-C to stop", file=sys.stderr)
        if args.dashboard:
            from .dashboard import Dashboard

            dcfg = settings.toml.get("dashboard", {})
            dash = Dashboard(settings, eng.storage, str(dcfg.get("host", "127.0.0.1")), int(args.port or dcfg.get("port", 8787)),
                             extra=lambda: {"last_scan": eng.last_report.summary() if eng.last_report else None, "limiter": eng.limiter.describe()})
            dash.start()
            print(f"dashboard: {dash.url}", file=sys.stderr)
        await eng.run(once=False)
    finally:
        if dash:
            dash.stop()
        await eng.close()


async def cmd_watch(args, settings):
    """Session-2 gate: watch live books over WebSocket and compare with REST."""
    from .ws import BookFeed

    eng = _engine(args, settings)
    feed = BookFeed(settings.ws_url, eng.signer)
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(list(args.ticker), stop, subscribe_fills=False))
    deadline = time.time() + float(args.minutes) * 60
    mismatches = 0
    try:
        while time.time() < deadline and not task.done():
            await asyncio.sleep(float(args.every))
            for t in args.ticker:
                ws_book = feed.book(t)
                rest = await eng.client.orderbook(t)
                line = {"ticker": t, "ws": ws_book.to_dict(3) if ws_book else None, "rest_yes_bid": str(rest.best_yes_bid), "rest_yes_ask": str(rest.best_yes_ask),
                        "frames": feed.state.frames, "gaps": feed.state.gaps}
                if ws_book and (ws_book.best_yes_bid != rest.best_yes_bid or ws_book.best_yes_ask != rest.best_yes_ask):
                    mismatches += 1
                    line["MISMATCH"] = True
                print(json.dumps(line, default=str))
        if task.done() and task.exception():
            raise task.exception()  # type: ignore[misc]
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        await eng.close()
    print(f"done: frames={feed.state.frames} gaps={feed.state.gaps} top-of-book mismatches={mismatches} (transient mismatches are normal; persistent ones are not)")


async def cmd_positions(args, settings):
    settings.require_credentials()
    eng = _engine(args, settings)
    try:
        ps = await eng.client.positions(settlement_status="unsettled")
        _print([{"ticker": p.ticker, "position": str(p.position), "exposure": str(p.market_exposure), "realized_pnl": str(p.realized_pnl),
                 "fees_paid": str(p.fees_paid), "resting_orders": p.resting_orders_count} for p in ps if p.position != 0])
    finally:
        await eng.close()


async def cmd_orders(args, settings):
    settings.require_credentials()
    eng = _engine(args, settings)
    try:
        os_ = await eng.client.orders(status=args.status)
        _print([{"order_id": o.order_id, "ticker": o.ticker, "status": o.status, "side": o.book_side, "yes_price": str(o.yes_price),
                 "remaining": str(o.remaining_count), "filled": str(o.fill_count), "created": o.created} for o in os_])
    finally:
        await eng.close()


async def cmd_cancel_all(args, settings):
    settings.require_credentials()
    if not args.yes:
        raise ConfigError("refusing without --yes")
    eng = _engine(args, settings)
    try:
        _print({"result": await eng.client.cancel_all_orders()})
    finally:
        await eng.close()


async def cmd_flatten(args, settings):
    settings.require_credentials()
    if not args.yes:
        raise ConfigError("refusing without --yes")
    eng = _engine(args, settings, trade=True)
    try:
        snap = await eng.account.refresh(force=True)
        res = await eng.exec_trade.flatten_all(snap.open_positions(), "manual flatten")  # type: ignore[union-attr]
        _print([{"ticker": r.leg.ticker, "side": r.leg.book_side, "filled": str(r.filled), "remaining": str(r.remaining), "error": r.error} for r in res])
    finally:
        await eng.close()


async def cmd_backtest(args, settings):
    storage = Storage(settings.db_path)
    if not args.offline:
        eng = _engine(args, settings)
        try:
            n = await sync_results(storage, eng.client)
            print(f"synced {n} settlement results", file=sys.stderr)
        finally:
            await eng.close()
    rep = run_backtest(storage, since_days=float(args.days))
    storage.close()
    out = rep.to_dict()
    print("WHAT WOULD MAKE THIS WRONG:", file=sys.stderr)
    for c in rep.caveats:
        print(" -", c, file=sys.stderr)
    _print(out)


async def cmd_review(args, settings):
    from .review import weekly_review

    storage = Storage(settings.db_path)
    try:
        _print(weekly_review(storage, days=float(args.days)))
    finally:
        storage.close()


async def cmd_gaps(args, settings):
    storage = Storage(settings.db_path)
    try:
        _print(storage.arb_gaps(since=time.time() - float(args.hours) * 3600, limit=int(args.limit)))
    finally:
        storage.close()


async def cmd_decisions(args, settings):
    storage = Storage(settings.db_path)
    try:
        _print(storage.decisions(since=time.time() - float(args.hours) * 3600, strategy=args.strategy, limit=int(args.limit)))
    finally:
        storage.close()


async def cmd_dashboard(args, settings):
    from .dashboard import Dashboard

    storage = Storage(settings.db_path)
    dcfg = settings.toml.get("dashboard", {})
    d = Dashboard(settings, storage, str(dcfg.get("host", "127.0.0.1")), int(args.port or dcfg.get("port", 8787)))
    d.start()
    print(f"dashboard: {d.url} (Ctrl-C to stop)", file=sys.stderr)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        d.stop()
        storage.close()


async def cmd_setup(args, settings):
    """Guided first run: keys -> .env -> connection check, in plain language."""
    import os

    from .setup_cmd import run_setup

    run_setup(settings.root)
    fresh_env = {k: v for k, v in os.environ.items() if not k.startswith("KALSHI_") and k != "CONFIRM_LIVE"}
    settings = load_settings(settings.root, environ=fresh_env)
    print("\nChecking the connection to Kalshi demo...")
    eng = _engine(args, settings)
    try:
        report = await eng.doctor()
        working = [pr for pr in report["probes"] if pr.get("status_code") == 200]
        if not working:
            print("Could not reach any Kalshi host. Details:")
            _print(report["probes"])
            print("Check your internet connection, then run `uv run bot doctor` again.")
            return
        if working[0]["rest"] != settings.rest_base_url:
            print(f"Note: the configured host did not answer but {working[0]['rest']} did. Put that pair into [hosts.demo] in config/bot.toml.")
        auth = report.get("auth", {})
        if not auth.get("ok"):
            print(f"Kalshi reached, but the key was rejected: {auth.get('error')}")
            print("Usual causes: a production key entered as the demo key, a key deleted on the site, or a computer clock that is off.")
            return
        print(f"Setup complete. Demo balance: ${auth['balance_cents'] / 100:.2f}. Positions value: ${auth['portfolio_value_cents'] / 100:.2f}.")
        print("Next: `uv run bot scan -v` for one observe-only pass, then `uv run bot run --dashboard` to keep observing.")
    finally:
        await eng.close()


def cmd_halt(args, settings):
    halt_mod.engage(settings.halt_path, args.reason or "manual")
    print(f"HALT engaged at {settings.halt_path}. All order placement is blocked until `bot resume --file`.")


def cmd_resume(args, settings):
    from .risk import RiskEngine

    done = []
    if args.file or not (args.daily or args.weekly or args.full_stop):
        done.append(f"HALT file removed: {halt_mod.release(settings.halt_path)}")
    if args.daily or args.weekly or args.full_stop:
        storage = Storage(settings.db_path)
        r = RiskEngine(storage, settings)
        for kind, flag in (("daily", args.daily), ("weekly", args.weekly), ("full_stop", args.full_stop)):
            if flag:
                r.manual_resume(kind)
                done.append(f"{kind} halt cleared")
        storage.close()
    print("\n".join(done))


# ---- parser ----------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bot", description="Kalshi event-contract trading system (demo by default, observe-only by default)")
    p.add_argument("--live", action="store_true", help="use production; also requires CONFIRM_LIVE=yes in the environment")
    p.add_argument("--root", help="repo root (defaults to the directory containing BRIEF.md)")
    p.add_argument("--quiet", action="store_true", help="no console log lines (file log still written)")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup", help="guided first run: enter keys, write .env, check the connection")
    sub.add_parser("balance", help="authenticate and print the balance")
    sub.add_parser("doctor", help="probe Kalshi hosts, auth, and rate limits")
    s = sub.add_parser("status", help="risk state, halts, positions")
    s.add_argument("--limit", default=20)
    s.add_argument("--offline", action="store_true")
    s = sub.add_parser("markets", help="list markets")
    s.add_argument("--series")
    s.add_argument("--event")
    s.add_argument("--status", default="open")
    s.add_argument("--limit", default=50)
    s = sub.add_parser("events", help="list open events for a series")
    s.add_argument("--series", required=True)
    s = sub.add_parser("book", help="print order books")
    s.add_argument("ticker", nargs="+")
    s.add_argument("--depth", default=5)
    s = sub.add_parser("series", help="series metadata incl. fee parameters")
    s.add_argument("ticker")
    s = sub.add_parser("scan", help="one observe-only pass of every enabled strategy")
    s.add_argument("--strategy", action="append")
    s.add_argument("-v", "--verbose", action="store_true")
    s = sub.add_parser("run", help="continuous loop (observe-only unless --trade)")
    s.add_argument("--trade", action="store_true", help="send orders for strategies whose config mode is 'trade'")
    s.add_argument("--strategy", action="append")
    s.add_argument("--interval", type=float)
    s.add_argument("--dashboard", action="store_true")
    s.add_argument("--port", type=int)
    s.add_argument("--no-ws", action="store_true", help="REST polling only")
    s = sub.add_parser("watch", help="WebSocket book watch vs REST (Session 2 gate)")
    s.add_argument("ticker", nargs="+")
    s.add_argument("--minutes", default=10)
    s.add_argument("--every", default=15)
    sub.add_parser("positions")
    s = sub.add_parser("orders")
    s.add_argument("--status", default="resting")
    s = sub.add_parser("cancel-all")
    s.add_argument("--yes", action="store_true")
    s = sub.add_parser("flatten", help="close every open position with reduce-only limit orders")
    s.add_argument("--yes", action="store_true")
    s = sub.add_parser("backtest", help="score logged decisions against settlements")
    s.add_argument("--days", default=30)
    s.add_argument("--offline", action="store_true")
    s = sub.add_parser("review", help="weekly calibration review; proposes only")
    s.add_argument("--days", default=7)
    s = sub.add_parser("gaps", help="detected ladder gaps")
    s.add_argument("--hours", default=48)
    s.add_argument("--limit", default=200)
    s = sub.add_parser("decisions", help="decision log")
    s.add_argument("--hours", default=24)
    s.add_argument("--strategy")
    s.add_argument("--limit", default=200)
    s = sub.add_parser("dashboard", help="status page with HALT button")
    s.add_argument("--port", type=int)
    s = sub.add_parser("halt", help="create the HALT file")
    s.add_argument("reason", nargs="?")
    s = sub.add_parser("resume", help="remove the HALT file and/or clear loss halts")
    s.add_argument("--file", action="store_true")
    s.add_argument("--daily", action="store_true")
    s.add_argument("--weekly", action="store_true")
    s.add_argument("--full-stop", action="store_true")
    return p


COMMANDS = {"setup": cmd_setup, "balance": cmd_balance, "doctor": cmd_doctor, "status": cmd_status, "markets": cmd_markets, "events": cmd_events, "book": cmd_book,
            "series": cmd_series, "scan": cmd_scan, "run": cmd_run, "watch": cmd_watch, "positions": cmd_positions, "orders": cmd_orders,
            "cancel-all": cmd_cancel_all, "flatten": cmd_flatten, "backtest": cmd_backtest, "review": cmd_review, "gaps": cmd_gaps,
            "decisions": cmd_decisions, "dashboard": cmd_dashboard}
SYNC_COMMANDS = {"halt": cmd_halt, "resume": cmd_resume}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = _settings(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    setup_logging(settings.root, settings.env, args.log_level, quiet=args.quiet or args.cmd in ("halt", "resume", "status", "gaps", "decisions", "setup"))
    _banner(settings)
    try:
        if args.cmd in SYNC_COMMANDS:
            SYNC_COMMANDS[args.cmd](args, settings)
        else:
            asyncio.run(COMMANDS[args.cmd](args, settings))
        return 0
    except UnexpectedApiResponse as e:
        print(f"STOP: the API returned something the docs don't describe. Not guessing.\n{e}", file=sys.stderr)
        return 3
    except BotError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
