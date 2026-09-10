"""Shared builders for offline tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kalshi_bot.account import AccountSnapshot
from kalshi_bot.config import Settings
from kalshi_bot.models import Balance, ExchangeStatus, Market, Position

NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)


def settings(tmp_path, env="demo"):
    return Settings(env=env, root=tmp_path, rest_base_url="https://demo.test/trade-api/v2", ws_url="wss://demo.test/trade-api/ws/v2",
                    api_key_id="kid", private_key_path=None, alert_webhook_url=None, db_path=tmp_path / "x.db",
                    halt_path=tmp_path / "HALT", toml={})


def market(ticker="KXHIGHNY-26SEP10-B70", event="KXHIGHNY-26SEP10", close=NOW + timedelta(hours=6), category="Climate and Weather",
           yes_bid="0.42", yes_ask="0.45", strike_type="between", floor="70", cap="71", **extra):
    d = {"ticker": ticker, "event_ticker": event, "status": "open", "close_time": close.isoformat(), "category": category,
         "yes_bid_dollars": yes_bid, "yes_ask_dollars": yes_ask, "strike_type": strike_type, "floor_strike": floor, "cap_strike": cap}
    d.update(extra)
    return Market.parse(d)


def position(ticker, qty, event=None):
    d = {"ticker": ticker, "position_fp": str(qty), "realized_pnl_dollars": "0", "fees_paid_dollars": "0"}
    if event:
        d["event_ticker"] = event
    return Position.parse(d)


def snapshot(balance_cents=100_000, portfolio_cents=0, realized_cents=0, positions=(), trading_active=True):
    return AccountSnapshot(ts=0.0, balance=Balance(balance_cents, portfolio_cents, None), positions=list(positions),
                           realized_cents=realized_cents, exchange=ExchangeStatus(True, trading_active, None))
