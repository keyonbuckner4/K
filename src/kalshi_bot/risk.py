"""RiskEngine: the single chokepoint every order passes through.

The limits below are copied from BRIEF.md and are deliberately not configurable at runtime.
Change them by editing BRIEF.md and this file together, nowhere else.

* 1% of account equity max per position (cost basis of the intent).
* 3% max daily realized loss -> flatten and halt until the next trading day (America/New_York).
* 6% max weekly realized loss -> halt until manually re-enabled (``bot resume --weekly``).
* 10% peak-to-trough drawdown of equity -> full stop, manual restart (``bot resume --full-stop``).
* Max 5 concurrent open positions; max 2 in the same underlying event.
* No entry inside the final 10 minutes before settlement unless the strategy is a settlement strategy.
* Limit orders only (the client cannot express a market order; prices are validated here too).
* A HALT file in the repo root blocks all order placement. This module is the only place that checks it.
* First 30 days live: max $25 per position regardless of the risk math.
* Daily and weekly loss counters persist to SQLite; restarts do not reset them.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from .account import AccountSnapshot
from .alerts import Alerts
from .config import Settings
from .errors import Halted, RiskRejected
from .halt import halt_active
from .intent import CLOSE, Intent
from .storage import Storage

log = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - missing tz database
    log.error("America/New_York timezone unavailable; daily/weekly keys fall back to UTC")
    ET = timezone.utc  # type: ignore[assignment]


@dataclass(frozen=True)
class RiskLimits:
    max_position_fraction: Decimal = Decimal("0.01")
    max_daily_loss_fraction: Decimal = Decimal("0.03")
    max_weekly_loss_fraction: Decimal = Decimal("0.06")
    max_drawdown_fraction: Decimal = Decimal("0.10")
    max_open_positions: int = 5
    max_positions_per_event: int = 2
    min_minutes_to_settlement: int = 10
    live_probation_days: int = 30
    live_probation_cap_cents: int = 2500
    blocked_categories: tuple[str, ...] = ("sport", "politic", "election", "culture", "entertainment")


LIMITS = RiskLimits()


def day_key(now: datetime) -> str:
    return now.astimezone(ET).date().isoformat()


def week_key(now: datetime) -> str:
    iso = now.astimezone(ET).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class HealthReport:
    ok: bool
    halted_reason: str | None
    flatten_required: bool
    equity_cents: int
    peak_equity_cents: int
    drawdown_fraction: Decimal
    daily_pnl_cents: int
    weekly_pnl_cents: int
    daily_limit_cents: int
    weekly_limit_cents: int
    open_positions: int
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Approval:
    intent: Intent
    max_cost_cents: int
    equity_cents: int
    per_position_cap_cents: int
    notes: list[str] = field(default_factory=list)


class RiskEngine:
    def __init__(self, storage: Storage, settings: Settings, alerts: Alerts | None = None, limits: RiskLimits = LIMITS,
                 blocked_categories: tuple[str, ...] | None = None, clock: Callable[[], datetime] = now_utc):
        self.storage = storage
        self.settings = settings
        self.alerts = alerts
        self.limits = limits
        self.blocked = tuple(c.lower() for c in (blocked_categories or limits.blocked_categories))
        self.clock = clock

    # ---- persisted state -------------------------------------------------------------------
    def _get(self, key: str, default: Any = None) -> Any:
        return self.storage.get_state(key, default)

    def _set(self, key: str, value: Any) -> None:
        self.storage.set_state(key, value)

    def _alert(self, level: str, title: str, message: str = "") -> None:
        if self.alerts:
            self.alerts.queue(level, title, message)
        else:
            log.log(logging.CRITICAL if level == "critical" else logging.WARNING, "ALERT %s: %s %s", level, title, message)

    def status(self) -> dict[str, Any]:
        st = self.storage.all_state()
        st["halt_file"] = halt_active(self.settings.halt_path)
        return st

    def manual_resume(self, kind: str) -> None:
        if kind == "daily":
            self._set("daily_halt_day", None)
        elif kind == "weekly":
            self._set("weekly_halt", False)
        elif kind == "full_stop":
            self._set("full_stop", False)
            # a manual restart re-bases the drawdown peak at the current equity
            self._set("peak_equity_cents", None)
        else:
            raise ValueError(kind)

    # ---- periodic health -----------------------------------------------------------------------
    def refresh(self, snapshot: AccountSnapshot, now: datetime | None = None) -> HealthReport:
        """Roll baselines, update the peak, and trip halts. Call on every scan, not just before orders."""
        now = now or self.clock()
        equity = snapshot.equity_cents
        realized = snapshot.realized_cents
        dk, wk = day_key(now), week_key(now)
        details: dict[str, Any] = {}

        if self._get("day_key") != dk:
            self._set("day_key", dk)
            self._set("day_baseline_realized_cents", realized)
            self._set("day_baseline_equity_cents", equity)
            if self._get("daily_halt_day") not in (None, dk):
                self._set("daily_halt_day", None)  # a new day releases the daily halt
        if self._get("week_key") != wk:
            self._set("week_key", wk)
            self._set("week_baseline_realized_cents", realized)
            self._set("week_baseline_equity_cents", equity)

        peak = self._get("peak_equity_cents")
        if peak is None or equity > peak:
            peak = equity
            self._set("peak_equity_cents", peak)

        daily_pnl = realized - int(self._get("day_baseline_realized_cents", realized))
        weekly_pnl = realized - int(self._get("week_baseline_realized_cents", realized))
        day_eq = int(self._get("day_baseline_equity_cents", equity) or 0)
        week_eq = int(self._get("week_baseline_equity_cents", equity) or 0)
        daily_limit = int(Decimal(day_eq) * self.limits.max_daily_loss_fraction)
        weekly_limit = int(Decimal(week_eq) * self.limits.max_weekly_loss_fraction)
        drawdown = (Decimal(peak - equity) / Decimal(peak)) if peak > 0 else Decimal("0")

        flatten = False
        reason: str | None = None
        if drawdown >= self.limits.max_drawdown_fraction and not self._get("full_stop"):
            self._set("full_stop", True)
            self._set("full_stop_reason", f"drawdown {drawdown:.2%} from peak {peak} to {equity} at {now.isoformat()}")
            flatten = True
            self._alert("critical", "FULL STOP: drawdown limit hit", self._get("full_stop_reason"))
        if weekly_limit > 0 and weekly_pnl <= -weekly_limit and not self._get("weekly_halt"):
            self._set("weekly_halt", True)
            self._set("weekly_halt_reason", f"weekly realized {weekly_pnl}c <= -{weekly_limit}c ({wk})")
            flatten = True
            self._alert("critical", "WEEKLY HALT", self._get("weekly_halt_reason"))
        if daily_limit > 0 and daily_pnl <= -daily_limit and self._get("daily_halt_day") != dk:
            self._set("daily_halt_day", dk)
            self._set("daily_halt_reason", f"daily realized {daily_pnl}c <= -{daily_limit}c ({dk})")
            flatten = True
            self._alert("critical", "DAILY HALT: flattening", self._get("daily_halt_reason"))

        if self._get("full_stop"):
            reason = f"full stop: {self._get('full_stop_reason')}"
        elif self._get("weekly_halt"):
            reason = f"weekly halt: {self._get('weekly_halt_reason')}"
        elif self._get("daily_halt_day") == dk:
            reason = f"daily halt: {self._get('daily_halt_reason')}"
        elif halt_active(self.settings.halt_path):
            reason = "HALT file present"
        elif snapshot.exchange is not None and not snapshot.exchange.trading_active:
            reason = "exchange trading is not active"

        self.storage.snapshot_equity(snapshot.balance_cents, snapshot.balance.portfolio_value_cents, realized)
        details.update({"day_key": dk, "week_key": wk, "realized_cents": realized})
        return HealthReport(ok=reason is None, halted_reason=reason, flatten_required=flatten, equity_cents=equity,
                            peak_equity_cents=int(peak), drawdown_fraction=drawdown, daily_pnl_cents=daily_pnl,
                            weekly_pnl_cents=weekly_pnl, daily_limit_cents=daily_limit, weekly_limit_cents=weekly_limit,
                            open_positions=len(self.logical_positions(snapshot)), details=details)

    # ---- position accounting -----------------------------------------------------------------
    def logical_positions(self, snapshot: AccountSnapshot) -> dict[str, set[str]]:
        """Map event_ticker -> set of logical position ids. A multi-leg intent (an arb basket) is
        one logical position; a market with no recorded intent counts as its own."""
        out: dict[str, set[str]] = {}
        held = snapshot.open_positions()
        by_ticker = self.storage.intents_for_markets([p.ticker for p in held])
        for p in held:
            ids = by_ticker.get(p.ticker) or {p.ticker}
            out.setdefault(p.event_ticker or p.ticker, set()).update(ids)
        return out

    # ---- the chokepoint -----------------------------------------------------------------------
    def approve(self, intent: Intent, snapshot: AccountSnapshot, now: datetime | None = None) -> Approval:
        """Every order goes through here. Raises Halted or RiskRejected with the reason; never guesses."""
        now = now or self.clock()
        notes: list[str] = []

        # 1. Kill switches. The HALT file is checked here and nowhere else.
        if halt_active(self.settings.halt_path):
            raise Halted("HALT file present in repo root")
        health = self.refresh(snapshot, now)
        if not health.ok:
            raise Halted(health.halted_reason or "halted")

        if not intent.legs:
            raise RiskRejected("intent has no legs")

        # 2. Limit orders only, with sane prices. (There is no market-order code path at all.)
        for leg in intent.legs:
            if leg.book_side not in ("bid", "ask"):
                raise RiskRejected(f"{leg.ticker}: book_side must be bid/ask, got {leg.book_side!r}")
            if not (Decimal("0.01") <= leg.price <= Decimal("0.99")):
                raise RiskRejected(f"{leg.ticker}: limit price {leg.price} outside [0.01, 0.99]")
            if leg.count <= 0:
                raise RiskRejected(f"{leg.ticker}: count must be positive")

        # 3. Non-goals: blocked categories.
        cats = [intent.category or ""] + [(l.market.category or "") for l in intent.legs if l.market]
        for c in cats:
            cl = c.lower()
            if any(b in cl for b in self.blocked):
                raise RiskRejected(f"category {c!r} is blocked by BRIEF.md non-goals")

        # 4. Settlement window.
        if not intent.settlement_strategy and not intent.reduce_only:
            for leg in intent.legs:
                st = leg.market.settle_time if leg.market else None
                if st is None:
                    raise RiskRejected(f"{leg.ticker}: settlement time unknown; refusing to enter")
                minutes = (st - now).total_seconds() / 60
                if minutes < self.limits.min_minutes_to_settlement:
                    raise RiskRejected(f"{leg.ticker}: settles in {minutes:.1f} min (< {self.limits.min_minutes_to_settlement})")

        equity = snapshot.equity_cents
        cost = intent.max_cost_cents
        cap = int(Decimal(equity) * self.limits.max_position_fraction)

        if intent.reduce_only or intent.kind == CLOSE:
            notes.append("reduce-only intent: size and count limits waived")
            return Approval(intent, 0, equity, cap, notes)

        # 5. Position size: 1% of equity, and the $25 probation cap for the first 30 live days.
        if self.settings.is_live:
            first = self._get("live_first_trade_ts")
            first_dt = datetime.fromtimestamp(first, tz=timezone.utc) if first else now
            if now - first_dt < timedelta(days=self.limits.live_probation_days):
                cap = min(cap, self.limits.live_probation_cap_cents)
                notes.append(f"live probation cap ${self.limits.live_probation_cap_cents / 100:.0f}")
        if cap <= 0:
            raise RiskRejected(f"equity {equity}c allows no position (1% cap = {cap}c)")
        if cost > cap:
            raise RiskRejected(f"cost {cost}c exceeds per-position cap {cap}c (equity {equity}c)")
        if cost > snapshot.balance_cents:
            raise RiskRejected(f"cost {cost}c exceeds available balance {snapshot.balance_cents}c (no leverage)")

        # 6. Concurrency: max 5 logical positions, max 2 per event.
        lp = self.logical_positions(snapshot)
        total = sum(len(s) for s in lp.values())
        if total + 1 > self.limits.max_open_positions:
            raise RiskRejected(f"{total} open positions; adding one exceeds max {self.limits.max_open_positions}")
        in_event = len(lp.get(intent.event_ticker, set()))
        if in_event + 1 > self.limits.max_positions_per_event:
            raise RiskRejected(f"{in_event} positions already in event {intent.event_ticker}; max {self.limits.max_positions_per_event}")

        if self.settings.is_live and not self._get("live_first_trade_ts"):
            self._set("live_first_trade_ts", time.time())
        return Approval(intent, cost, equity, cap, notes)
