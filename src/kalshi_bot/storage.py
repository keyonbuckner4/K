"""SQLite persistence. One file per environment (demo/live). Every decision is stored with its
reason, including rejections, so the log is the audit trail the BRIEF asks for."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    strategy TEXT NOT NULL,
    event_ticker TEXT,
    market_ticker TEXT,
    book_side TEXT,
    price TEXT,
    count TEXT,
    model_prob TEXT,
    edge_gross_cents TEXT,
    fee_cents TEXT,
    edge_net_cents TEXT,
    accepted INTEGER NOT NULL,
    stage TEXT NOT NULL,
    reason TEXT NOT NULL,
    details TEXT
);
CREATE INDEX IF NOT EXISTS decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS decisions_market ON decisions(market_ticker, ts);
CREATE INDEX IF NOT EXISTS decisions_stage_market ON decisions(stage, market_ticker, ts);

CREATE TABLE IF NOT EXISTS quotes (
    ts REAL NOT NULL,
    market_ticker TEXT NOT NULL,
    yes_bid TEXT, yes_ask TEXT, yes_bid_size TEXT, yes_ask_size TEXT,
    close_time TEXT
);
CREATE INDEX IF NOT EXISTS quotes_market ON quotes(market_ticker, ts);

CREATE TABLE IF NOT EXISTS arb_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    event_ticker TEXT NOT NULL,
    kind TEXT NOT NULL,
    n_legs INTEGER NOT NULL,
    sum_price TEXT NOT NULL,
    gross_edge_cents TEXT NOT NULL,
    fee_cents TEXT NOT NULL,
    net_edge_cents TEXT NOT NULL,
    size TEXT NOT NULL,
    legs TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS arb_gaps_ts ON arb_gaps(ts);

CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    strategy TEXT NOT NULL,
    event_ticker TEXT,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    expected_edge_cents TEXT,
    max_cost_cents INTEGER,
    legs TEXT NOT NULL,
    result TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    client_order_id TEXT,
    intent_id TEXT,
    ts REAL NOT NULL,
    market_ticker TEXT NOT NULL,
    book_side TEXT NOT NULL,
    price TEXT NOT NULL,
    count TEXT NOT NULL,
    time_in_force TEXT,
    status TEXT,
    fill_count TEXT,
    remaining_count TEXT,
    average_fill_price TEXT,
    fee_paid TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS orders_intent ON orders(intent_id);

CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT,
    ts REAL NOT NULL,
    market_ticker TEXT NOT NULL,
    book_side TEXT,
    outcome_side TEXT,
    price TEXT,
    count TEXT,
    fee TEXT,
    is_taker INTEGER,
    raw TEXT
);

CREATE TABLE IF NOT EXISTS risk_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts REAL NOT NULL,
    balance_cents INTEGER NOT NULL,
    portfolio_value_cents INTEGER NOT NULL,
    equity_cents INTEGER NOT NULL,
    realized_cents INTEGER
);

CREATE TABLE IF NOT EXISTS series_cache (
    ticker TEXT PRIMARY KEY,
    fetched REAL NOT NULL,
    raw TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_results (
    market_ticker TEXT PRIMARY KEY,
    event_ticker TEXT,
    result TEXT,
    settled_ts REAL,
    fetched REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS forecasts (
    ts REAL NOT NULL,
    strategy TEXT NOT NULL,
    key TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS forecasts_key ON forecasts(strategy, key, ts);
"""


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if is_dataclass(o):
        return asdict(o)
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return str(o)


def dumps(o: Any) -> str:
    return json.dumps(o, default=_json_default, sort_keys=True)


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


_NUMS = re.compile(r"-?\d+(?:\.\d+)?")
THROTTLED_STAGES = ("model", "ladder", "window")   # per-market model views: one row per material change, plus a heartbeat


class LogThrottle:
    """Suppress rows that repeat the previous row for the same key within ``heartbeat`` seconds.

    The bot prices every open market on every scan (about 1,400 markets every 30-60 s). Writing a row each
    time was 2-3 million rows a day, which made the scorecard's read window miss every settled market.
    A row is written when the material content changes, or when the last row is older than the heartbeat."""

    def __init__(self, heartbeat_sec: float = 1800.0):
        self.heartbeat = float(heartbeat_sec)
        self._last: dict[Any, tuple[Any, float]] = {}

    def should_write(self, key: Any, signature: Any, now: float) -> bool:
        prev = self._last.get(key)
        if prev is not None and prev[0] == signature and now - prev[1] < self.heartbeat:
            return False
        self._last[key] = (signature, now)
        return True

    def forget(self) -> None:
        self._last.clear()


def _cents(v: Any) -> str | None:
    """Price-like value rounded to a cent, so tick-level noise below a cent does not count as a change."""
    if v is None:
        return None
    try:
        return f"{Decimal(str(v)):.2f}"
    except Exception:
        return str(v)


class Storage:
    def __init__(self, path: Path | str, log_heartbeat_sec: float = 1800.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.decision_throttle = LogThrottle(log_heartbeat_sec)
        self.quote_throttle = LogThrottle(log_heartbeat_sec)
        self.suppressed = {"decisions": 0, "quotes": 0}

    def close(self) -> None:
        self.conn.close()

    # ---- decisions ------------------------------------------------------------------
    def log_decision(self, strategy: str, stage: str, accepted: bool, reason: str, *, event_ticker: str | None = None,
                     market_ticker: str | None = None, book_side: str | None = None, price: Any = None, count: Any = None,
                     model_prob: Any = None, edge_gross_cents: Any = None, fee_cents: Any = None, edge_net_cents: Any = None,
                     details: Any = None, ts: float | None = None) -> int:
        ts = ts or time.time()
        if stage in THROTTLED_STAGES:
            key = (strategy, stage, event_ticker, market_ticker)
            sig = (bool(accepted), _NUMS.sub("#", reason), _cents(model_prob), _cents(price), book_side)
            if not self.decision_throttle.should_write(key, sig, ts):
                self.suppressed["decisions"] += 1
                return -1
        cur = self.conn.execute(
            "INSERT INTO decisions(ts,strategy,event_ticker,market_ticker,book_side,price,count,model_prob,edge_gross_cents,"
            "fee_cents,edge_net_cents,accepted,stage,reason,details) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, strategy, event_ticker, market_ticker, book_side, _s(price), _s(count), _s(model_prob),
             _s(edge_gross_cents), _s(fee_cents), _s(edge_net_cents), 1 if accepted else 0, stage, reason,
             dumps(details) if details is not None else None),
        )
        return int(cur.lastrowid)

    def latest_model_decisions(self, since: float) -> list[dict[str, Any]]:
        """The newest priced ('model' stage with a model probability) row per market since ``since``.
        Done in SQL so the answer does not depend on how many rows the table holds."""
        rows = self.conn.execute(
            "SELECT d.* FROM decisions d JOIN (SELECT market_ticker, MAX(ts) AS ts FROM decisions"
            " WHERE stage = 'model' AND model_prob IS NOT NULL AND market_ticker IS NOT NULL AND ts >= ? GROUP BY market_ticker) l"
            " ON l.market_ticker = d.market_ticker AND l.ts = d.ts WHERE d.stage = 'model' AND d.model_prob IS NOT NULL"
            " GROUP BY d.market_ticker ORDER BY d.ts", (since,))
        return [dict(r) for r in rows]

    def latest_candidate_decisions(self, since: float) -> list[dict[str, Any]]:
        """The newest accepted model candidate row per market since ``since`` (what the pessimistic P&L is scored on)."""
        rows = self.conn.execute(
            "SELECT d.* FROM decisions d JOIN (SELECT market_ticker, MAX(ts) AS ts FROM decisions"
            " WHERE stage = 'model' AND accepted = 1 AND model_prob IS NOT NULL AND market_ticker IS NOT NULL AND ts >= ? GROUP BY market_ticker) l"
            " ON l.market_ticker = d.market_ticker AND l.ts = d.ts WHERE d.stage = 'model' AND d.accepted = 1"
            " GROUP BY d.market_ticker ORDER BY d.ts", (since,))
        return [dict(r) for r in rows]

    def execute_decisions(self, since: float) -> list[dict[str, Any]]:
        """Accepted execute-stage rows (observed or placed) since ``since``."""
        rows = self.conn.execute("SELECT * FROM decisions WHERE stage = 'execute' AND accepted = 1 AND ts >= ? ORDER BY ts", (since,))
        return [dict(r) for r in rows]

    def prune(self, decisions_days: float, quotes_days: float, now: float | None = None) -> dict[str, int]:
        """Delete decision rows older than ``decisions_days`` and quote rows older than ``quotes_days``."""
        now = time.time() if now is None else now
        out = {}
        for table, days in (("decisions", decisions_days), ("quotes", quotes_days)):
            if days and days > 0:
                out[table] = self.conn.execute(f"DELETE FROM {table} WHERE ts < ?", (now - days * 86400,)).rowcount
        return out

    def compact(self, bucket_sec: float = 1800.0) -> dict[str, Any]:
        """One-time cleanup of rows written before throttling existed: keep the newest model/ladder row per
        market (or event) per ``bucket_sec`` window and the newest quote per market per window, then VACUUM.
        Run only while the bot is stopped (the CLI takes the instance lock first)."""
        before = {"decisions": self.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
                  "quotes": self.conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], "bytes": self.path.stat().st_size}
        self.conn.execute("BEGIN")
        self.conn.execute(
            "DELETE FROM decisions WHERE stage IN ('model', 'ladder') AND id NOT IN (SELECT MAX(id) FROM decisions WHERE stage IN ('model', 'ladder')"
            " GROUP BY strategy, stage, event_ticker, market_ticker, CAST(ts / ? AS INTEGER))", (bucket_sec,))
        self.conn.execute(
            "DELETE FROM quotes WHERE rowid NOT IN (SELECT MAX(rowid) FROM quotes GROUP BY market_ticker, CAST(ts / ? AS INTEGER))", (bucket_sec,))
        self.conn.execute("COMMIT")
        self.conn.execute("VACUUM")
        after = {"decisions": self.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
                 "quotes": self.conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], "bytes": self.path.stat().st_size}
        return {"before": before, "after": after}

    def decisions(self, since: float | None = None, strategy: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        q = "SELECT * FROM decisions WHERE 1=1"
        args: list[Any] = []
        if since is not None:
            q += " AND ts >= ?"
            args.append(since)
        if strategy:
            q += " AND strategy = ?"
            args.append(strategy)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(q, args)]

    # ---- quotes / gaps / forecasts ----------------------------------------------------
    def log_quote(self, market_ticker: str, yes_bid: Any, yes_ask: Any, yes_bid_size: Any, yes_ask_size: Any,
                  close_time: Any = None, ts: float | None = None) -> None:
        ts = ts or time.time()
        if not self.quote_throttle.should_write(market_ticker, (_cents(yes_bid), _cents(yes_ask)), ts):
            self.suppressed["quotes"] += 1
            return
        self.conn.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?,?)",
                          (ts, market_ticker, _s(yes_bid), _s(yes_ask), _s(yes_bid_size), _s(yes_ask_size),
                           close_time.isoformat() if hasattr(close_time, "isoformat") else _s(close_time)))

    def log_arb_gap(self, event_ticker: str, kind: str, n_legs: int, sum_price: Any, gross_edge_cents: Any, fee_cents: Any,
                    net_edge_cents: Any, size: Any, legs: Any, ts: float | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO arb_gaps(ts,event_ticker,kind,n_legs,sum_price,gross_edge_cents,fee_cents,net_edge_cents,size,legs)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts or time.time(), event_ticker, kind, n_legs, str(sum_price), str(gross_edge_cents), str(fee_cents),
             str(net_edge_cents), str(size), dumps(legs)))
        return int(cur.lastrowid)

    def arb_gaps(self, since: float | None = None, limit: int = 500) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM arb_gaps", []
        if since is not None:
            q += " WHERE ts >= ?"
            args.append(since)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(q, args)]

    def log_forecast(self, strategy: str, key: str, payload: Any, ts: float | None = None) -> None:
        self.conn.execute("INSERT INTO forecasts VALUES (?,?,?,?)", (ts or time.time(), strategy, key, dumps(payload)))

    # ---- intents / orders / fills -----------------------------------------------------
    def save_intent(self, intent_id: str, strategy: str, event_ticker: str | None, status: str, mode: str,
                    expected_edge_cents: Any, max_cost_cents: int | None, legs: Any, result: Any = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO intents(intent_id,ts,strategy,event_ticker,status,mode,expected_edge_cents,max_cost_cents,legs,result)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (intent_id, time.time(), strategy, event_ticker, status, mode, _s(expected_edge_cents), max_cost_cents, dumps(legs),
             dumps(result) if result is not None else None))

    def update_intent(self, intent_id: str, status: str, result: Any = None) -> None:
        self.conn.execute("UPDATE intents SET status=?, result=COALESCE(?, result) WHERE intent_id=?",
                          (status, dumps(result) if result is not None else None, intent_id))

    def intents(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute("SELECT * FROM intents WHERE status=? ORDER BY ts DESC LIMIT ?", (status, limit))
        else:
            rows = self.conn.execute("SELECT * FROM intents ORDER BY ts DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["legs"] = json.loads(d["legs"])
            out.append(d)
        return out

    OPEN_INTENT_STATUSES = ("filled", "partial", "open", "placed")

    def intents_for_markets(self, tickers: Iterable[str]) -> dict[str, set[str]]:
        """Map each held market ticker to every open intent that holds it."""
        wanted = set(tickers)
        out: dict[str, set[str]] = {t: set() for t in wanted}
        q = f"SELECT intent_id, legs FROM intents WHERE status IN ({','.join('?' * len(self.OPEN_INTENT_STATUSES))})"
        for r in self.conn.execute(q, self.OPEN_INTENT_STATUSES):
            for leg in json.loads(r["legs"]):
                t = leg.get("ticker")
                if t in wanted:
                    out[t].add(str(r["intent_id"]))
        return out

    def intent_for_market(self, market_ticker: str) -> str | None:
        ids = self.intents_for_markets([market_ticker]).get(market_ticker) or set()
        return sorted(ids)[0] if ids else None

    def close_intents_for_market(self, market_ticker: str, status: str = "closed") -> int:
        n = 0
        for t, ids in self.intents_for_markets([market_ticker]).items():
            for iid in ids:
                self.conn.execute("UPDATE intents SET status=? WHERE intent_id=?", (status, iid))
                n += 1
        return n

    def save_order(self, order_id: str, client_order_id: str | None, intent_id: str | None, market_ticker: str, book_side: str,
                   price: Any, count: Any, time_in_force: str | None, status: str | None, fill_count: Any = None,
                   remaining_count: Any = None, average_fill_price: Any = None, fee_paid: Any = None, raw: Any = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO orders(order_id,client_order_id,intent_id,ts,market_ticker,book_side,price,count,time_in_force,"
            "status,fill_count,remaining_count,average_fill_price,fee_paid,raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, client_order_id, intent_id, time.time(), market_ticker, book_side, str(price), str(count), time_in_force,
             status, _s(fill_count), _s(remaining_count), _s(average_fill_price), _s(fee_paid), dumps(raw) if raw is not None else None))

    def update_order(self, order_id: str, status: str | None = None, fill_count: Any = None, remaining_count: Any = None,
                     raw: Any = None) -> None:
        self.conn.execute(
            "UPDATE orders SET status=COALESCE(?,status), fill_count=COALESCE(?,fill_count), remaining_count=COALESCE(?,remaining_count),"
            " raw=COALESCE(?,raw) WHERE order_id=?",
            (status, _s(fill_count), _s(remaining_count), dumps(raw) if raw is not None else None, order_id))

    def orders(self, intent_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if intent_id:
            rows = self.conn.execute("SELECT * FROM orders WHERE intent_id=? ORDER BY ts DESC LIMIT ?", (intent_id, limit))
        else:
            rows = self.conn.execute("SELECT * FROM orders ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def save_fill(self, fill_id: str, order_id: str | None, market_ticker: str, book_side: str | None, outcome_side: str | None,
                  price: Any, count: Any, fee: Any, is_taker: bool | None, raw: Any = None, ts: float | None = None) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO fills(fill_id,order_id,ts,market_ticker,book_side,outcome_side,price,count,fee,is_taker,raw)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fill_id, order_id, ts or time.time(), market_ticker, book_side, outcome_side, _s(price), _s(count), _s(fee),
             None if is_taker is None else int(bool(is_taker)), dumps(raw) if raw is not None else None))
        return cur.rowcount > 0

    def fills(self, since: float | None = None, limit: int = 500) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM fills", []
        if since is not None:
            q += " WHERE ts >= ?"
            args.append(since)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(q, args)]

    # ---- risk state -------------------------------------------------------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM risk_state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO risk_state(key,value,updated) VALUES (?,?,?)", (key, dumps(value), time.time()))

    def all_state(self) -> dict[str, Any]:
        return {r["key"]: json.loads(r["value"]) for r in self.conn.execute("SELECT key, value FROM risk_state")}

    def snapshot_equity(self, balance_cents: int, portfolio_value_cents: int, realized_cents: int | None = None) -> None:
        self.conn.execute("INSERT INTO equity_snapshots VALUES (?,?,?,?,?)",
                          (time.time(), balance_cents, portfolio_value_cents, balance_cents + portfolio_value_cents, realized_cents))

    def equity_history(self, limit: int = 500) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM equity_snapshots ORDER BY ts DESC LIMIT ?", (limit,))]

    # ---- caches -----------------------------------------------------------------------
    def cache_series(self, ticker: str, raw: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO series_cache VALUES (?,?,?)", (ticker, time.time(), dumps(raw)))

    def cached_series(self, ticker: str, max_age: float) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT fetched, raw FROM series_cache WHERE ticker=?", (ticker,)).fetchone()
        if not row or time.time() - row["fetched"] > max_age:
            return None
        return json.loads(row["raw"])

    def save_market_result(self, market_ticker: str, event_ticker: str | None, result: str | None, settled_ts: float | None) -> None:
        self.conn.execute("INSERT OR REPLACE INTO market_results VALUES (?,?,?,?,?)",
                          (market_ticker, event_ticker, result, settled_ts, time.time()))

    def market_results(self, tickers: Iterable[str] | None = None) -> dict[str, str | None]:
        if tickers is None:
            rows = self.conn.execute("SELECT market_ticker, result FROM market_results")
        else:
            tl = list(tickers)
            if not tl:
                return {}
            rows = self.conn.execute(f"SELECT market_ticker, result FROM market_results WHERE market_ticker IN ({','.join('?' * len(tl))})", tl)
        return {r["market_ticker"]: r["result"] for r in rows}

    def unresolved_decision_markets(self, since: float | None = None) -> list[str]:
        q = ("SELECT DISTINCT market_ticker FROM decisions WHERE market_ticker IS NOT NULL AND model_prob IS NOT NULL"
             " AND market_ticker NOT IN (SELECT market_ticker FROM market_results WHERE result IN ('yes', 'no'))")
        args: list[Any] = []
        if since is not None:
            q += " AND ts >= ?"
            args.append(since)
        return [r["market_ticker"] for r in self.conn.execute(q, args)]
