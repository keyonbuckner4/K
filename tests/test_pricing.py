import math

from kalshi_bot.pricing import INF, market_condition, norm_cdf, prob_yes_lognormal, prob_yes_normal, real_interval

from helpers import market


def test_norm_cdf():
    assert abs(norm_cdf(0) - 0.5) < 1e-12
    assert abs(norm_cdf(1.959964) - 0.975) < 1e-5
    assert norm_cdf(-40) == 0.0 and norm_cdf(40) == 1.0


def test_conditions_from_strike_types():
    assert market_condition(market(strike_type="greater", floor="85", cap=None)) is not None
    c = market_condition(market(strike_type="less", floor=None, cap="64"))
    assert c.lo == -INF and c.hi == 64 and not c.hi_inclusive
    c = market_condition(market(strike_type="between", floor="70", cap="71"))
    assert (c.lo, c.hi, c.lo_inclusive, c.hi_inclusive) == (70, 71, True, True)
    assert market_condition(market(strike_type="custom", floor=None, cap=None)) is None


def test_integer_continuity_correction():
    c = market_condition(market(strike_type="between", floor="70", cap="71"))
    assert real_interval(c, True) == (69.5, 71.5)
    assert real_interval(c, False) == (70, 71)
    g = market_condition(market(strike_type="greater", floor="85", cap=None))
    assert real_interval(g, True) == (85.5, INF)
    ge = market_condition(market(strike_type="greater_or_equal", floor="86", cap=None))
    assert real_interval(ge, True) == (85.5, INF)
    l = market_condition(market(strike_type="less", floor=None, cap="64"))
    assert real_interval(l, True) == (-INF, 63.5)
    half = market_condition(market(strike_type="greater", floor="85.5", cap=None))
    assert real_interval(half, True) == (85.5, INF)  # non-integer strikes are left alone


def test_ladder_probabilities_sum_to_one():
    ms = [market(ticker="T1", strike_type="less_or_equal", floor=None, cap="69"),
          market(ticker="T2", strike_type="between", floor="70", cap="71"),
          market(ticker="T3", strike_type="between", floor="72", cap="73"),
          market(ticker="T4", strike_type="greater", floor="73", cap=None)]
    ps = [prob_yes_normal(m, 71.2, 2.5, True) for m in ms]
    assert abs(sum(ps) - 1.0) < 1e-9
    assert ps[1] > ps[2] > ps[0]


def test_lognormal_barrier():
    above = market(ticker="B", strike_type="greater", floor="100000", cap=None)
    p_atm = prob_yes_lognormal(above, 100000, 0.5, 1 / 365)
    assert 0.49 < p_atm < 0.5  # driftless lognormal: slightly below one half at the money
    p_deep = prob_yes_lognormal(above, 120000, 0.5, 1 / 365)
    assert p_deep > 0.99
    assert prob_yes_lognormal(above, 0, 0.5, 1) is None
    between = market(ticker="R", strike_type="between", floor="99000", cap="101000")
    assert 0 < prob_yes_lognormal(between, 100000, 0.5, 1 / 365) < 1
