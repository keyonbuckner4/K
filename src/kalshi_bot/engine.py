"""The main loop: fetch -> strategies -> entry gate -> RiskEngine -> Executor, on a timer.

Observe-only unless ``trade=True`` AND the strategy's config mode is ``trade`` AND credentials
exist. Every scan refreshes the account, re-checks the loss limits, and logs every decision.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .account import AccountSnapshot, AccountView
from .alerts import Alerts
from .auth import KalshiSigner
from .backtest import activity_stats, run_backtest, sync_results
from .client import KalshiClient
from .config import LIVE, Settings
from .errors import ApiError, ConfigError, DataUnavailable, UnexpectedApiResponse
from .execution import OBSERVE, TRADE, Executor
from .fees import FeeSchedule
from .gate import GateConfig, check_intent
from .intent import Intent
from .models import Event, Fill
from .orderbook import OrderBook
from .ratelimit import SharedRateLimiter
from .risk import RiskEngine
from .storage import Storage
from .strategies import build_strategies
from .strategies.base import ScanContext, Strategy
from .ws import BookFeed

log = logging.getLogger(__name__)

# Host families Kalshi has published. `bot doctor` probes all of them.
KNOWN_HOSTS = {
    "demo": [("https://external-api.demo.kalshi.co/trade-api/v2", "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"),
             ("https://demo-api.kalshi.co/trade-api/v2", "wss://demo-api.kalshi.co/trade-api/ws/v2")],
    "live": [("https://external-api.kalshi.com/trade-api/v2", "wss://external-api-ws.kalshi.com/trade-api/ws/v2"),
             ("https://api.elections.kalshi.com/trade-api/v2", "wss://api.elections.kalshi.com/trade-api/ws/v2")],
}


@dataclass
class ScanReport:
    started: float
    finished: float = 0.0
    strategies: int = 0
    events: int = 0
    markets: int = 0
    intents: int = 0
    gated_out: int = 0
    executed: list[dict[str, Any]] = field(default_factory=list)
    health: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        ex = ", ".join(f"{e['status']}:{e['intent_id']}" for e in self.executed) or "-"
        return (f"scan {self.finished - self.started:.1f}s: {self.strategies} strategies, {self.events} events, {self.markets} markets, "
                f"{self.intents} intents, {self.gated_out} gated out, executed [{ex}]"
                + (f", errors: {self.errors}" if self.errors else ""))


_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def reason_histogram(decisions: list[dict[str, Any]], top: int = 12) -> list[tuple[str, int]]:
    """Group logged decisions by strategy, stage and reason with numbers blanked out, most common first."""
    c: Counter[str] = Counter()
    for d in decisions:
        reason = _NUM.sub("#", str(d.get("reason", "")))
        reason = reason.split(" (")[0][:90]
        c[f"{d.get('strategy')}/{d.get('stage')}: {reason}"] += 1
    return c.most_common(top)


class Engine:
    def __init__(self, settings: Settings, *, trade: bool = False, strategies: list[str] | None = None, transport: Any = None,
                 feeds: dict[str, Any] | None = None, use_ws: bool = True):
        self.settings = settings
        self.trade_flag = trade
        self.selected = set(strategies or [])
        self.transport = transport
        self.feeds = feeds or {}
        self.use_ws = use_ws
        cfg = settings.toml
        self.cfg = cfg
        self.storage = Storage(settings.db_path)
        self.limiter = SharedRateLimiter.from_config(cfg.get("ratelimit"))
        self.signer = KalshiSigner.from_pem_file(settings.api_key_id, settings.private_key_path) if settings.has_credentials else None
        self.client = KalshiClient(settings, self.limiter, self.signer, transport=transport)
        # Market data may come from production's public endpoints while the account stays on demo.
        # Demo books are nearly empty, so observe-only runs learn nothing from them. Orders are
        # always priced from the venue they are sent to: trade mode forces data_env == env.
        eng_cfg = cfg.get("engine", {})
        requested = str(eng_cfg.get("market_data", "auto")).lower()
        if requested not in ("auto", "demo", "live"):
            raise ConfigError(f"[engine] market_data must be auto, demo or live, got {requested!r}")
        if requested == "auto":
            self.data_env = settings.env if trade else LIVE
        else:
            self.data_env = requested
        if trade and self.data_env != settings.env:
            raise ConfigError(f"[engine] market_data = {requested!r} cannot be combined with --trade on {settings.env}: "
                              "orders must be priced from the venue they are sent to")
        hosts = cfg.get("hosts", {}).get(self.data_env) or {}
        if self.data_env != settings.env and requested == "auto" and not (hosts.get("rest") and hosts.get("ws")):
            log.warning("[hosts.%s] missing from config/bot.toml; reading market data from %s instead", self.data_env, settings.env)
            self.data_env = settings.env
        if self.data_env == settings.env:
            self.data_client = self.client
        else:
            if not hosts.get("rest") or not hosts.get("ws"):
                raise ConfigError(f"config/bot.toml needs [hosts.{self.data_env}] to read market data from {self.data_env}")
            data_settings = dataclasses.replace(settings, env=self.data_env, rest_base_url=str(hosts["rest"]).rstrip("/"), ws_url=str(hosts["ws"]),
                                                api_key_id=None, private_key_path=None)
            self.data_client = KalshiClient(data_settings, self.limiter, None, transport=transport)
        self.alerts = Alerts(settings.alert_webhook_url, str(cfg.get("alerts", {}).get("min_level", "warning")), settings.env)
        self.fee_sched = FeeSchedule.from_config(cfg.get("fees"))
        self.gate_cfg = GateConfig.from_toml(cfg.get("gate"))
        eng = cfg.get("engine", {})
        self.risk = RiskEngine(self.storage, settings, self.alerts, blocked_categories=tuple(eng.get("blocked_categories", ())) or None)
        self.account = AccountView(self.client, float(eng.get("positions_cache_ttl_sec", 5)))
        ex = cfg.get("execution", {})
        self.exec_observe = Executor(self.client, self.storage, self.risk, mode=OBSERVE, alerts=self.alerts)
        self.exec_trade = Executor(self.client, self.storage, self.risk, mode=TRADE, time_in_force=str(ex.get("time_in_force", "immediate_or_cancel")),
                                   stp=str(ex.get("self_trade_prevention_type", "taker_at_cross")), alerts=self.alerts) if trade else None
        self.strategies: list[Strategy] = [s for s in build_strategies(cfg, self.storage, **self.feeds) if not self.selected or s.name in self.selected]
        self.scan_interval = float(eng.get("scan_interval_sec", 30))
        self.series_ttl = float(eng.get("series_cache_ttl_sec", 21600))
        self.market_ttl = float(eng.get("market_cache_ttl_sec", 20))
        self.max_events = int(eng.get("max_events_per_series", 6))
        self._events_cache: dict[str, tuple[float, list[Event]]] = {}
        self._missing_series: set[str] = set()
        self._empty_warned: dict[str, float] = {}
        # The WebSocket needs a key for the venue it connects to; with production data on a demo key, poll REST instead.
        self.feed: BookFeed | None = BookFeed(settings.ws_url, self.signer, on_fill=self._on_ws_fill) if (use_ws and self.data_client is self.client) else None
        self._feed_task: asyncio.Task | None = None
        self._feed_tickers: list[str] = []
        self.last_report: ScanReport | None = None
        self.last_fill_sync = time.time() - 3600
        self.score_interval = float(eng_cfg.get("score_interval_sec", 3600))
        self._last_score = 0.0
        self.stop = asyncio.Event()

    # ---- lifecycle ------------------------------------------------------------------------------
    @property
    def can_trade(self) -> bool:
        return self.exec_trade is not None and self.signer is not None

    def executor_for(self, strategy: Strategy) -> Executor:
        if self.can_trade and strategy.mode == TRADE:
            return self.exec_trade  # type: ignore[return-value]
        return self.exec_observe

    async def startup(self) -> dict[str, Any]:
        info: dict[str, Any] = {"env": self.settings.env, "rest": self.settings.rest_base_url, "ws": self.settings.ws_url,
                                "market_data_env": self.data_env, "market_data_rest": self.data_client.base_url,
                                "trade_flag": self.trade_flag, "credentials": self.signer is not None,
                                "strategies": {s.name: s.mode for s in self.strategies}, "db": str(self.settings.db_path)}
        status = await self.client.exchange_status()
        info["exchange"] = {"exchange_active": status.exchange_active, "trading_active": status.trading_active}
        if self.signer is not None:
            try:
                limits = await self.client.account_limits()
                self.limiter.update_limits(limits)
            except (ApiError, UnexpectedApiResponse) as e:
                log.warning("GET /account/limits unusable (%s); using conservative fallback limits", e)
            bal = await self.client.balance()
            info["balance_cents"] = bal.balance_cents
            info["portfolio_value_cents"] = bal.portfolio_value_cents
        info["limiter"] = self.limiter.describe()
        log.info("startup: %s", info)
        return info

    async def close(self) -> None:
        self.stop.set()
        if self._feed_task:
            self._feed_task.cancel()
            try:
                await self._feed_task
            except (asyncio.CancelledError, Exception):
                pass
        await self.alerts.flush()
        await self.client.close()
        if self.data_client is not self.client:
            await self.data_client.close()
        await self.alerts.close()
        for s in self.strategies:
            closer = getattr(getattr(s, "nws", None), "close", None) or getattr(getattr(s, "feed", None), "close", None)
            if closer:
                try:
                    await closer()
                except Exception:
                    pass
        self.storage.close()

    # ---- data ------------------------------------------------------------------------------------
    async def ensure_series_fee(self, series: str) -> bool:
        if self.fee_sched.known(series):
            return True
        if series in self._missing_series:
            return False
        cached = self.storage.cached_series(series, self.series_ttl)
        if cached is None:
            try:
                s = await self.data_client.series(series)
            except ApiError as e:
                if e.status == 404:
                    log.error("series %s not found on Kalshi %s (check config tickers)", series, self.data_env)
                    self._missing_series.add(series)
                    return False
                raise
            self.storage.cache_series(series, s.raw)
            cached = s.raw
        from .models import Series
        try:
            self.fee_sched.register_series(Series.parse(cached))
        except DataUnavailable as e:
            log.error("%s", e)
            self._missing_series.add(series)
            return False
        return True

    async def events_for(self, series: str) -> list[Event]:
        c = self._events_cache.get(series)
        if c and time.time() - c[0] < self.market_ttl:
            return c[1]
        try:
            events = await self.data_client.events(series_ticker=series, status="open", with_nested_markets=True)
        except ApiError as e:
            if e.status == 404:
                self._missing_series.add(series)
                return []
            raise
        now = datetime.now(timezone.utc)
        total = len(events)
        all_events = events
        events = [e for e in events if any(m.is_open() for m in e.markets)]
        events.sort(key=lambda e: min((m.settle_time for m in e.markets if m.settle_time), default=now))
        events = events[: self.max_events]
        n_markets = sum(1 for e in events for m in e.markets if m.is_open())
        if not events:
            seen = sorted({m.status for e in all_events for m in e.markets})
            level = logging.WARNING if time.time() - self._empty_warned.get(series, 0) > 3600 else logging.DEBUG
            self._empty_warned[series] = time.time() if level == logging.WARNING else self._empty_warned[series]
            log.log(level, "series %s: Kalshi %s returned %d events and none with open markets (market statuses seen: %s); nothing to scan",
                    series, self.data_env, total, seen)
        else:
            log.info("series %s: %d open events, %d open markets (of %d events returned)", series, len(events), n_markets, total)
        self._events_cache[series] = (time.time(), events)
        return events

    async def books_for(self, tickers: list[str]) -> dict[str, OrderBook]:
        out: dict[str, OrderBook] = {}
        missing: list[str] = []
        for t in tickers:
            b = self.feed.fresh_book(t, self.market_ttl) if self.feed else None
            if b is not None:
                out[t] = b
            else:
                missing.append(t)
        if missing:
            try:
                out.update(await self.data_client.orderbooks(missing))
            except (ApiError, UnexpectedApiResponse) as e:
                log.error("orderbook fetch failed for %d tickers: %s", len(missing), e)
        return out

    async def _on_ws_fill(self, fill: Fill) -> None:
        self.storage.save_fill(fill.fill_id, fill.order_id, fill.ticker, fill.book_side, fill.outcome_side, fill.yes_price, fill.count,
                               fill.fee_cost, fill.is_taker, fill.raw)

    async def sync_fills(self) -> int:
        if self.signer is None:
            return 0
        try:
            fills = await self.client.fills(min_ts=int(self.last_fill_sync) - 60)
        except (ApiError, UnexpectedApiResponse) as e:
            log.warning("fill sync failed: %s", e)
            return 0
        n = 0
        for f in fills:
            if self.storage.save_fill(f.fill_id, f.order_id, f.ticker, f.book_side, f.outcome_side, f.yes_price, f.count, f.fee_cost, f.is_taker, f.raw,
                                      ts=f.created.timestamp() if f.created else None):
                n += 1
        self.last_fill_sync = time.time()
        return n

    # ---- one scan ----------------------------------------------------------------------------------
    async def scan_once(self) -> ScanReport:
        rep = ScanReport(started=time.time())
        now = datetime.now(timezone.utc)
        snapshot: AccountSnapshot | None = None
        if self.signer is not None:
            snapshot = await self.account.refresh(force=True)
            health = self.risk.refresh(snapshot, now)
            rep.health = {"ok": health.ok, "reason": health.halted_reason, "equity_cents": health.equity_cents, "daily_pnl_cents": health.daily_pnl_cents,
                          "weekly_pnl_cents": health.weekly_pnl_cents, "drawdown": f"{health.drawdown_fraction:.2%}", "open_positions": health.open_positions}
            if health.flatten_required:
                log.critical("risk requires flattening: %s", health.halted_reason)
                if self.exec_trade is not None:
                    await self.exec_trade.flatten_all(snapshot.open_positions(), health.halted_reason or "loss limit")
                else:
                    self.storage.log_decision("engine", "flatten", False, f"observe mode: flatten requested but not executed ({health.halted_reason})")
            await self.sync_fills()

        all_tickers: list[str] = []
        for strat in self.strategies:
            if not strat.enabled:
                continue
            rep.strategies += 1
            events: list[Event] = []
            for series in strat.series():
                try:
                    if not await self.ensure_series_fee(series):
                        continue
                    events.extend(await self.events_for(series))
                except UnexpectedApiResponse:
                    raise  # CLAUDE.md: stop and report, do not guess
                except ApiError as e:
                    rep.errors.append(f"{series}: {e}")
                    log.error("fetch failed for series %s: %s", series, e)
            markets = [m for e in events for m in e.markets if m.is_open()]
            tickers = [m.ticker for m in markets]
            all_tickers.extend(tickers)
            books = await self.books_for(tickers)
            for m in markets:
                b = books.get(m.ticker)
                if b:
                    self.storage.log_quote(m.ticker, b.best_yes_bid, b.best_yes_ask, b.yes_bid_size, b.yes_ask_size, m.settle_time)
            rep.events += len(events)
            rep.markets += len(markets)
            ctx = ScanContext(now, events, books, self.fee_sched, self.gate_cfg, self.storage)
            try:
                intents = await strat.scan(ctx)
            except UnexpectedApiResponse:
                raise
            except Exception as e:  # a strategy bug must not kill the loop; it is logged loudly
                log.exception("strategy %s failed", strat.name)
                rep.errors.append(f"{strat.name}: {e}")
                continue
            rep.intents += len(intents)
            for intent in intents:
                results = check_intent(intent, books, self.fee_sched, now, self.gate_cfg)
                failed = [r for r in results if not r.ok]
                for r in results:
                    self.storage.log_decision(strat.name, "gate", r.ok, r.reason, event_ticker=intent.event_ticker,
                                              market_ticker=r.details.get("ticker"), book_side=r.details.get("book_side"),
                                              price=r.details.get("price"), count=r.details.get("count"), fee_cents=r.details.get("fee_cents_per_contract"),
                                              edge_net_cents=r.details.get("edge_net_cents", intent.expected_edge_cents), details={"intent_id": intent.intent_id, **r.details})
                if failed:
                    rep.gated_out += 1
                    continue
                if snapshot is None:
                    self.storage.log_decision(strat.name, "execute", False, "no credentials: intent passed the gate but cannot be risk-checked or placed",
                                              event_ticker=intent.event_ticker, details=intent.to_dict())
                    rep.executed.append({"intent_id": intent.intent_id, "status": "no_credentials"})
                    continue
                result = await self.executor_for(strat).execute(intent, snapshot, now)
                rep.executed.append({"intent_id": intent.intent_id, "status": result.status, "reason": result.reason, "strategy": strat.name})
                if result.status in ("filled", "partial", "unwound"):
                    snapshot = await self.account.refresh(force=True)
        await self.alerts.flush()
        self._feed_tickers = sorted(set(all_tickers))
        rep.finished = time.time()
        self.last_report = rep
        log.info(rep.summary())
        return rep

    # ---- model scorecard ---------------------------------------------------------------------------------
    async def score_models(self, since_days: float = 30.0) -> dict[str, Any]:
        """Pull settlement results for every market the models priced, re-score them, and store the
        scorecard (Brier vs market, calibration, pessimistic P&L, caveats) for the dashboard and `bot status`."""
        synced = await sync_results(self.storage, self.data_client)
        rep = run_backtest(self.storage, since_days=since_days)
        card = rep.to_dict()
        card.update({"ts": time.time(), "synced_results": synced, "activity": activity_stats(self.storage, 7.0)})
        self.storage.set_state("last_backtest", card)
        self._last_score = time.time()
        log.info("model scorecard: %d scored, brier model=%s market=%s, %d candidates, pnl %sc (pessimistic), %d unresolved",
                 rep.n_scored, f"{rep.brier_model:.4f}" if rep.brier_model is not None else "-",
                 f"{rep.brier_market:.4f}" if rep.brier_market is not None else "-", rep.n_candidates, rep.pnl_cents, rep.unresolved)
        return card

    # ---- loop ----------------------------------------------------------------------------------------
    async def run(self, once: bool = False) -> ScanReport:
        rep = await self.scan_once()
        if once:
            return rep
        while not self.stop.is_set():
            if time.time() - self._last_score >= self.score_interval:
                try:
                    await self.score_models()
                except UnexpectedApiResponse:
                    raise
                except Exception as e:  # scoring must never stop the scan loop
                    log.error("model scoring failed: %s", e)
                    self._last_score = time.time()
            if self.feed and self._feed_tickers:
                if self._feed_task is None or self._feed_task.done():
                    self._feed_task = asyncio.create_task(self.feed.run(self._feed_tickers, self.stop, subscribe_fills=self.signer is not None))
                elif self._feed_tickers != getattr(self.feed, "_tickers", []):
                    await self.feed.set_tickers(self._feed_tickers)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.scan_interval)
                break
            except asyncio.TimeoutError:
                pass
            try:
                rep = await self.scan_once()
            except UnexpectedApiResponse as e:
                log.critical("STOPPING: API returned something undocumented: %s", e)
                self.alerts.queue("critical", "engine stopped on unexpected API response", str(e)[:500])
                await self.alerts.flush()
                raise
        return rep

    # ---- diagnostics ----------------------------------------------------------------------------------
    async def doctor(self) -> dict[str, Any]:
        """Probe every known host family with an unsigned GET /exchange/status, then a signed balance call."""
        import httpx

        out: dict[str, Any] = {"env": self.settings.env, "configured": {"rest": self.settings.rest_base_url, "ws": self.settings.ws_url},
                               "key_source": self.settings.key_source, "credential_error": self.settings.credential_error, "probes": []}
        candidates = [(self.settings.rest_base_url, self.settings.ws_url)] + [h for h in KNOWN_HOSTS.get(self.settings.env, []) if h[0] != self.settings.rest_base_url]
        async with httpx.AsyncClient(timeout=10.0, transport=self.transport) as http:
            for rest, ws in candidates:
                probe: dict[str, Any] = {"rest": rest, "ws": ws}
                try:
                    r = await http.get(f"{rest}/exchange/status")
                    probe["status_code"] = r.status_code
                    probe["body"] = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]
                except httpx.HTTPError as e:
                    probe["error"] = str(e)
                out["probes"].append(probe)
        if self.signer is not None:
            try:
                bal = await self.client.balance()
                out["auth"] = {"ok": True, "balance_cents": bal.balance_cents, "portfolio_value_cents": bal.portfolio_value_cents}
            except Exception as e:
                out["auth"] = {"ok": False, "error": str(e)}
            try:
                lim = await self.client.account_limits()
                out["limits"] = {"tier": lim.usage_tier, "read": lim.read.__dict__, "write": lim.write.__dict__}
            except Exception as e:
                out["limits"] = {"error": str(e)}
        else:
            out["auth"] = {"ok": False, "error": self.settings.credential_error or "no credentials in .env"}
        return out
