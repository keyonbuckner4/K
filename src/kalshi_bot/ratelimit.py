"""One shared rate limiter in front of the whole account.

Kalshi meters reads and writes in separate token buckets (tiers are quoted in tokens per second,
a request costs 10 tokens by default; batch cancels cost 2 tokens per order). ``GET
/account/limits`` reports the live refill rate and capacity, which ``update_limits`` applies at
startup. Kalshi returns HTTP 429 with no Retry-After header, so the client pairs this limiter
with bounded, jittered backoff.

Design points from BRIEF.md:
* Exactly one instance is shared by every worker. Not one per strategy.
* A reserve of the write bucket is held back for cancels and risk actions: normal-priority
  writes never take the bucket below the reserve, critical writes may.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .models import AccountLimits, RateBudget

READ = "read"
WRITE = "write"
NORMAL = "normal"
CRITICAL = "critical"


class TokenBucket:
    def __init__(self, capacity: float, refill_per_sec: float, clock=time.monotonic):
        if capacity <= 0 or refill_per_sec <= 0:
            raise ValueError("capacity and refill must be positive")
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self.tokens = float(capacity)
        self._clock = clock
        self._last = clock()

    def refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)

    def available(self) -> float:
        self.refill()
        return self.tokens

    def wait_for(self, cost: float, floor: float = 0.0) -> float:
        """Seconds until ``cost`` tokens can be taken while leaving ``floor`` tokens behind."""
        self.refill()
        need = cost + floor - self.tokens
        return 0.0 if need <= 0 else need / self.refill_per_sec

    def take(self, cost: float) -> None:
        self.refill()
        self.tokens -= cost  # may go negative on purpose after a 429 drain

    def drain(self) -> None:
        self.refill()
        self.tokens = 0.0

    def reconfigure(self, capacity: float, refill_per_sec: float) -> None:
        self.refill()
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self.tokens = min(self.tokens, self.capacity)


@dataclass
class LimiterStats:
    read_waits: int = 0
    write_waits: int = 0
    total_wait_seconds: float = 0.0
    throttled_429: int = 0


class SharedRateLimiter:
    def __init__(self, read: RateBudget, write: RateBudget, default_cost: int = 10,
                 write_reserve_fraction: float = 0.3, clock=time.monotonic, sleep=None):
        if not (0.0 <= write_reserve_fraction < 1.0):
            raise ValueError("write_reserve_fraction must be in [0, 1)")
        self.default_cost = int(default_cost)
        self.write_reserve_fraction = float(write_reserve_fraction)
        self._read = TokenBucket(read.capacity, read.refill_per_sec, clock)
        self._write = TokenBucket(write.capacity, write.refill_per_sec, clock)
        self._locks = {READ: asyncio.Lock(), WRITE: asyncio.Lock()}
        self._sleep = sleep or asyncio.sleep
        self.stats = LimiterStats()
        self.source = "config"

    @classmethod
    def from_config(cls, cfg: dict | None) -> "SharedRateLimiter":
        cfg = cfg or {}
        cost = int(cfg.get("tokens_per_request", 10))
        read = RateBudget(int(cfg.get("fallback_read_tokens_per_sec", 200)), int(cfg.get("fallback_read_capacity", 400)))
        write = RateBudget(int(cfg.get("fallback_write_tokens_per_sec", 100)), int(cfg.get("fallback_write_capacity", 100)))
        return cls(read, write, cost, float(cfg.get("write_reserve_fraction", 0.3)))

    def update_limits(self, limits: AccountLimits) -> None:
        self._read.reconfigure(limits.read.capacity, limits.read.refill_per_sec)
        self._write.reconfigure(limits.write.capacity, limits.write.refill_per_sec)
        self.source = f"api:{limits.usage_tier}"

    def describe(self) -> dict:
        return {
            "source": self.source,
            "read": {"capacity": self._read.capacity, "refill_per_sec": self._read.refill_per_sec, "tokens": round(self._read.available(), 1)},
            "write": {"capacity": self._write.capacity, "refill_per_sec": self._write.refill_per_sec, "tokens": round(self._write.available(), 1),
                      "reserve": round(self._write.capacity * self.write_reserve_fraction, 1)},
            "default_cost": self.default_cost,
        }

    def _bucket(self, kind: str) -> TokenBucket:
        if kind == READ:
            return self._read
        if kind == WRITE:
            return self._write
        raise ValueError(f"unknown bucket kind {kind!r}")

    async def acquire(self, kind: str, cost: int | None = None, priority: str = NORMAL) -> float:
        """Block until ``cost`` tokens are available; return seconds waited.

        Normal writes leave the reserve untouched; critical writes (cancels, risk flattening)
        may spend it. Reads have no reserve.
        """
        cost_f = float(self.default_cost if cost is None else cost)
        bucket = self._bucket(kind)
        floor = 0.0
        if kind == WRITE and priority != CRITICAL:
            floor = bucket.capacity * self.write_reserve_fraction
        waited = 0.0
        async with self._locks[kind]:
            while True:
                w = bucket.wait_for(cost_f, floor)
                if w <= 0:
                    bucket.take(cost_f)
                    break
                waited += w
                await self._sleep(w)
        if waited > 0:
            if kind == READ:
                self.stats.read_waits += 1
            else:
                self.stats.write_waits += 1
            self.stats.total_wait_seconds += waited
        return waited

    def penalize(self, kind: str) -> None:
        """Called on HTTP 429: assume the server bucket is empty and drain ours."""
        self._bucket(kind).drain()
        self.stats.throttled_429 += 1
