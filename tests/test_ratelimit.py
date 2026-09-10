import asyncio

import pytest

from kalshi_bot import ratelimit
from kalshi_bot.models import AccountLimits, RateBudget


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(clock, reserve=0.3):
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        clock.t += s

    lim = ratelimit.SharedRateLimiter(RateBudget(200, 400), RateBudget(100, 100), 10, reserve, clock=clock, sleep=fake_sleep)
    return lim, slept


def test_reads_and_writes_are_separate_buckets():
    clock = FakeClock()
    lim, slept = make(clock)

    async def go():
        for _ in range(40):  # 400 tokens of read capacity
            await lim.acquire("read")
        assert slept == []
        await lim.acquire("read")  # 41st must wait for refill: 10 tokens at 200/s = 0.05s
        assert slept and abs(slept[-1] - 0.05) < 1e-9
        # write bucket untouched by reads
        assert lim.describe()["write"]["tokens"] == 100

    asyncio.run(go())


def test_normal_writes_never_consume_the_reserve_but_critical_can():
    clock = FakeClock()
    lim, slept = make(clock, reserve=0.3)

    async def go():
        # capacity 100, reserve 30 -> 7 normal writes of 10 tokens fit, the 8th waits
        for _ in range(7):
            await lim.acquire("write")
        assert slept == []
        waited = await lim.acquire("write")
        assert waited > 0
        # drain back to reserve level and show a critical write goes through immediately
        lim._write.tokens = 30.0
        waited = await lim.acquire("write", priority="critical")
        assert waited == 0
        assert lim._write.tokens == 20.0

    asyncio.run(go())


def test_update_limits_from_api_response():
    clock = FakeClock()
    lim, _ = make(clock)
    limits = AccountLimits.parse({"usage_tier": "advanced", "read": {"refill_rate": 300, "bucket_capacity": 600},
                                  "write": {"refill_rate": 300, "bucket_capacity": 600}})
    lim.update_limits(limits)
    d = lim.describe()
    assert d["read"]["refill_per_sec"] == 300 and d["write"]["capacity"] == 600 and d["source"] == "api:advanced"


def test_penalize_drains_bucket():
    clock = FakeClock()
    lim, slept = make(clock)

    async def go():
        lim.penalize("write")
        assert lim.stats.throttled_429 == 1
        await lim.acquire("write", priority="critical")
        assert slept and slept[-1] > 0  # had to wait for refill after the drain

    asyncio.run(go())


def test_from_config_defaults():
    lim = ratelimit.SharedRateLimiter.from_config({})
    d = lim.describe()
    assert d["read"]["capacity"] == 400 and d["write"]["reserve"] == 30.0 and d["default_cost"] == 10
    with pytest.raises(ValueError):
        ratelimit.SharedRateLimiter(RateBudget(1, 1), RateBudget(1, 1), 10, 1.5)
