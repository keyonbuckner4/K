"""Every BRIEF.md limit has a test here, including restart persistence."""

from datetime import timedelta
from decimal import Decimal

import pytest

from kalshi_bot import halt
from kalshi_bot.errors import ConfigError, Halted, RiskRejected
from kalshi_bot.intent import ARB, Intent, Leg
from kalshi_bot.risk import RiskEngine, day_key, week_key
from kalshi_bot.storage import Storage

from helpers import NOW, market, position, settings, snapshot


def engine(tmp_path, env="demo"):
    return RiskEngine(Storage(tmp_path / "x.db"), settings(tmp_path, env))


def intent(count=5, price="0.45", ticker="KXHIGHNY-26SEP10-B70", event="KXHIGHNY-26SEP10", m=None, kind="directional", **kw):
    m = m or market(ticker=ticker, event=event)
    return Intent("test", event, [Leg(ticker, "bid", Decimal(price), count, market=m, model_prob=Decimal("0.6"))], kind=kind, **kw)


def test_approves_a_normal_intent(tmp_path):
    e = engine(tmp_path)
    a = e.approve(intent(), snapshot(), NOW)
    assert a.max_cost_cents == 225 and a.per_position_cap_cents == 1000


def test_halt_file_blocks_everything(tmp_path):
    e = engine(tmp_path)
    halt.engage(e.settings.halt_path)
    with pytest.raises(Halted, match="HALT file"):
        e.approve(intent(), snapshot(), NOW)


def test_exchange_closed_blocks(tmp_path):
    e = engine(tmp_path)
    with pytest.raises(Halted, match="trading is not active"):
        e.approve(intent(), snapshot(trading_active=False), NOW)


def test_one_percent_per_position(tmp_path):
    e = engine(tmp_path)
    # equity $1000 -> cap $10 -> 22 contracts at 45c = $9.90 ok, 23 = $10.35 rejected
    e.approve(intent(count=22), snapshot(), NOW)
    with pytest.raises(RiskRejected, match="exceeds per-position cap"):
        e.approve(intent(count=23), snapshot(), NOW)


def test_live_probation_caps_at_25_dollars(tmp_path):
    e = engine(tmp_path, env="live")
    big = snapshot(balance_cents=10_000_000)  # $100k -> 1% is $1000, but probation caps at $25
    e.approve(intent(count=55), big, NOW)  # $24.75
    with pytest.raises(RiskRejected, match="cap 2500c"):
        e.approve(intent(count=56), big, NOW)
    # after 30 days the cap lifts (first live trade timestamp persisted)
    e.storage.set_state("live_first_trade_ts", (NOW - timedelta(days=31)).timestamp())
    e.approve(intent(count=56), big, NOW)


def test_no_leverage(tmp_path):
    e = engine(tmp_path)
    s = snapshot(balance_cents=100, portfolio_cents=100_000)  # rich in positions, no cash
    with pytest.raises(RiskRejected, match="available balance"):
        e.approve(intent(count=5), s, NOW)


def test_settlement_window(tmp_path):
    e = engine(tmp_path)
    soon = market(close=NOW + timedelta(minutes=9))
    with pytest.raises(RiskRejected, match="settles in 9.0 min"):
        e.approve(intent(m=soon), snapshot(), NOW)
    e.approve(intent(m=soon, settlement_strategy=True), snapshot(), NOW)
    e.approve(intent(m=market(close=NOW + timedelta(minutes=11))), snapshot(), NOW)


def test_blocked_categories(tmp_path):
    e = engine(tmp_path)
    for cat in ("Sports", "Politics", "Elections", "Pop Culture", "Entertainment"):
        with pytest.raises(RiskRejected, match="blocked"):
            e.approve(intent(m=market(category=cat)), snapshot(), NOW)
    e.approve(intent(m=market(category="Crypto")), snapshot(), NOW)


def test_limit_price_and_side_validation(tmp_path):
    e = engine(tmp_path)
    bad = intent()
    bad.legs[0].price = Decimal("0.995")
    with pytest.raises(RiskRejected, match="limit price"):
        e.approve(bad, snapshot(), NOW)
    bad.legs[0].price = Decimal("0.5")
    bad.legs[0].book_side = "market"
    with pytest.raises(RiskRejected, match="book_side"):
        e.approve(bad, snapshot(), NOW)


def test_max_five_positions_and_two_per_event(tmp_path):
    e = engine(tmp_path)
    five = [position(f"KXE{i}-1-A", 1, event=f"KXE{i}-1") for i in range(5)]
    with pytest.raises(RiskRejected, match="5 open positions"):
        e.approve(intent(), snapshot(positions=five), NOW)
    two_same = [position("KXHIGHNY-26SEP10-B70", 1), position("KXHIGHNY-26SEP10-B72", -1)]
    with pytest.raises(RiskRejected, match="already in event"):
        e.approve(intent(ticker="KXHIGHNY-26SEP10-B74"), snapshot(positions=two_same), NOW)
    # a basket recorded as one intent counts as one logical position
    e.storage.save_intent("basket1", "ladder_arb", "KXHIGHNY-26SEP10", "filled", "trade", 0, 0,
                          [{"ticker": "KXHIGHNY-26SEP10-B70"}, {"ticker": "KXHIGHNY-26SEP10-B72"}])
    e.approve(intent(ticker="KXHIGHNY-26SEP10-B74"), snapshot(positions=two_same), NOW)


def test_reduce_only_intents_bypass_size_and_count_limits(tmp_path):
    e = engine(tmp_path)
    five = [position(f"KXE{i}-1-A", 1, event=f"KXE{i}-1") for i in range(5)]
    close = Intent("test", "KXE0-1", [Leg("KXE0-1-A", "ask", Decimal("0.40"), 1, market=market(ticker="KXE0-1-A", event="KXE0-1"), reduce_only=True)], kind="close")
    a = e.approve(close, snapshot(positions=five), NOW)
    assert a.max_cost_cents == 0


def test_daily_loss_halts_and_persists_across_restart(tmp_path):
    e = engine(tmp_path)
    e.refresh(snapshot(balance_cents=100_000, realized_cents=0), NOW)      # baseline: equity $1000, realized 0
    r = e.refresh(snapshot(balance_cents=97_000, realized_cents=-3_000), NOW + timedelta(hours=1))
    assert not r.ok and r.flatten_required and "daily halt" in r.halted_reason
    with pytest.raises(Halted, match="daily halt"):
        e.approve(intent(), snapshot(balance_cents=97_000, realized_cents=-3_000), NOW + timedelta(hours=1))
    # restart: a new engine over the same DB is still halted, even if the account looks fine
    e2 = RiskEngine(Storage(tmp_path / "x.db"), settings(tmp_path))
    with pytest.raises(Halted, match="daily halt"):
        e2.approve(intent(), snapshot(balance_cents=100_000, realized_cents=0), NOW + timedelta(hours=2))
    # the next trading day (ET) releases it
    r = e2.refresh(snapshot(balance_cents=97_000, realized_cents=-3_000), NOW + timedelta(days=1))
    assert r.ok


def test_weekly_loss_requires_manual_resume(tmp_path):
    e = engine(tmp_path)
    e.refresh(snapshot(balance_cents=100_000, realized_cents=0), NOW)
    r = e.refresh(snapshot(balance_cents=94_000, realized_cents=-6_000), NOW + timedelta(days=1))
    assert "weekly halt" in r.halted_reason and r.flatten_required
    e2 = RiskEngine(Storage(tmp_path / "x.db"), settings(tmp_path))
    r = e2.refresh(snapshot(balance_cents=100_000, realized_cents=0), NOW + timedelta(days=2))
    assert "weekly halt" in r.halted_reason
    e2.manual_resume("weekly")
    assert e2.refresh(snapshot(balance_cents=100_000, realized_cents=0), NOW + timedelta(days=2)).ok


def test_drawdown_full_stop_requires_manual_restart(tmp_path):
    e = engine(tmp_path)
    e.refresh(snapshot(balance_cents=100_000), NOW)
    e.refresh(snapshot(balance_cents=120_000), NOW + timedelta(minutes=1))  # new peak $1200
    r = e.refresh(snapshot(balance_cents=108_000), NOW + timedelta(minutes=2))  # -10% from peak
    assert "full stop" in r.halted_reason and r.flatten_required
    e2 = RiskEngine(Storage(tmp_path / "x.db"), settings(tmp_path))
    assert "full stop" in e2.refresh(snapshot(balance_cents=200_000), NOW + timedelta(days=30)).halted_reason
    e2.manual_resume("full_stop")
    assert e2.refresh(snapshot(balance_cents=200_000), NOW + timedelta(days=30)).ok


def test_day_and_week_keys_are_eastern_time():
    late_utc = NOW.replace(hour=3)  # 03:00 UTC = 23:00 ET previous day
    assert day_key(late_utc) == "2026-09-09"
    assert day_key(NOW) == "2026-09-10"
    assert week_key(NOW) == "2026-W37"


def test_status_reports_state(tmp_path):
    e = engine(tmp_path)
    e.refresh(snapshot(), NOW)
    st = e.status()
    assert st["day_key"] == "2026-09-10" and st["halt_file"] is False and st["peak_equity_cents"] == 100_000


# ---- configurable limits ---------------------------------------------------------------------
def test_risk_limits_read_from_config_and_the_position_cap_follows():
    from kalshi_bot.risk import RiskLimits

    d = RiskLimits.from_toml(None)
    assert d.max_position_fraction == Decimal("0.01") and d.live_probation_cap_cents == 2500

    lim = RiskLimits.from_toml({"max_position_fraction": 0.05, "max_daily_loss_fraction": 0.15,
                                "max_weekly_loss_fraction": 0.25, "max_drawdown_fraction": 0.40,
                                "live_probation_cap_dollars": 50, "max_open_positions": 8, "max_positions_per_event": 3})
    assert lim.max_position_fraction == Decimal("0.05") and lim.live_probation_cap_cents == 5000
    assert lim.max_open_positions == 8 and "5.00%" in lim.describe()["max_position"]


def test_a_position_cap_that_would_self_destruct_is_refused():
    """A losing binary contract loses the whole position, so a 15% position against a 10% full stop
    means the first loss permanently halts the bot. That is a config error, not a surprise later."""
    from kalshi_bot.risk import RiskLimits

    with pytest.raises(ConfigError) as e:
        RiskLimits.from_toml({"max_position_fraction": 0.15})
    assert "max_drawdown_fraction" in str(e.value) and "permanently" in str(e.value)

    # raising the drawdown limit alone is still incoherent: one loss would end the day
    with pytest.raises(ConfigError, match="daily halt"):
        RiskLimits.from_toml({"max_position_fraction": 0.15, "max_drawdown_fraction": 0.50})

    # moving the whole ladder together is accepted; the operator is choosing to risk half the account
    lim = RiskLimits.from_toml({"max_position_fraction": 0.15, "max_daily_loss_fraction": 0.30,
                                "max_weekly_loss_fraction": 0.45, "max_drawdown_fraction": 0.50})
    assert lim.max_position_fraction == Decimal("0.15")

    with pytest.raises(ConfigError, match="fraction of equity"):
        RiskLimits.from_toml({"max_position_fraction": 15})      # 15 not 0.15
    with pytest.raises(ConfigError, match="weekly"):
        RiskLimits.from_toml({"max_daily_loss_fraction": 0.20, "max_weekly_loss_fraction": 0.10})
    with pytest.raises(ConfigError, match="max_positions_per_event"):
        RiskLimits.from_toml({"max_open_positions": 2, "max_positions_per_event": 3})


def test_configured_position_cap_is_what_the_engine_enforces(tmp_path):
    """The cap in force is the configured one, not a hardcoded 1%."""
    from kalshi_bot.risk import RiskLimits

    st = settings(tmp_path)
    storage = Storage(st.db_path)
    lim = RiskLimits.from_toml({"max_position_fraction": 0.05, "max_daily_loss_fraction": 0.15,
                                "max_weekly_loss_fraction": 0.25, "max_drawdown_fraction": 0.40})
    risk = RiskEngine(storage, st, limits=lim, clock=lambda: NOW)
    snap = snapshot(balance_cents=100_00)   # $100 equity
    risk.refresh(snap, NOW)
    # 5% of $100 is a $5.00 cap: 11 contracts at 45c = $4.95 passes, 12 = $5.40 does not.
    # Under the 1% default the cap would have been $1.00 and both would have been rejected.
    a = risk.approve(intent(count=11), snap, NOW)
    assert a.per_position_cap_cents == 500
    with pytest.raises(RiskRejected, match="exceeds per-position cap"):
        risk.approve(intent(count=12), snap, NOW)
    storage.close()
