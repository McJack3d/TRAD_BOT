"""Token-bucket rate limiter for IBKR API calls.

IBKR enforces per-API ceilings (the famous 50 messages / sec on orders,
~60 historical-data requests / 10 minutes, and so on) — if the bot blows
through them the gateway silently drops messages or, worse, disconnects.
This module gives a tiny async-friendly limiter that:

  * supports several named buckets simultaneously (orders, historical,
    market data lines, generic)
  * is Redis-backed when a URL is provided (so multiple workers share
    a single quota)
  * falls back to an in-memory bucket per process when Redis isn't
    configured — useful in tests, paper mode, single-process deploys

`acquire()` blocks until a token is free; it never raises.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass
class BucketSpec:
    capacity: int  # max tokens
    refill_per_second: float  # tokens added per second


def per_minute(n: int) -> BucketSpec:
    return BucketSpec(capacity=n, refill_per_second=n / 60.0)


def per_window(n: int, window_seconds: float) -> BucketSpec:
    return BucketSpec(capacity=n, refill_per_second=n / max(1e-3, window_seconds))


class _Limiter(Protocol):
    async def acquire(self, bucket: str, tokens: int = 1) -> None: ...

    async def close(self) -> None: ...


class InMemoryRateLimiter:
    """Single-process token bucket. Asyncio-safe (one lock per bucket)."""

    def __init__(self, specs: dict[str, BucketSpec]):
        self._specs = dict(specs)
        self._tokens: dict[str, float] = {b: s.capacity for b, s in specs.items()}
        self._last_refill: dict[str, float] = {b: time.monotonic() for b in specs}
        self._locks: dict[str, asyncio.Lock] = {b: asyncio.Lock() for b in specs}

    def _refill(self, bucket: str) -> None:
        spec = self._specs[bucket]
        now = time.monotonic()
        elapsed = now - self._last_refill[bucket]
        self._tokens[bucket] = min(
            spec.capacity, self._tokens[bucket] + elapsed * spec.refill_per_second
        )
        self._last_refill[bucket] = now

    async def acquire(self, bucket: str, tokens: int = 1) -> None:
        if bucket not in self._specs:
            raise KeyError(f"unknown rate-limit bucket: {bucket}")
        spec = self._specs[bucket]
        if tokens > spec.capacity:
            raise ValueError(
                f"requested {tokens} tokens > bucket capacity {spec.capacity}"
            )
        async with self._locks[bucket]:
            while True:
                self._refill(bucket)
                if self._tokens[bucket] >= tokens:
                    self._tokens[bucket] -= tokens
                    return
                deficit = tokens - self._tokens[bucket]
                wait = deficit / spec.refill_per_second
                await asyncio.sleep(max(0.01, wait))

    async def close(self) -> None:
        return None

    # Helper for tests — peek at remaining tokens without consuming.
    def available(self, bucket: str) -> float:
        self._refill(bucket)
        return self._tokens[bucket]


class RedisRateLimiter:
    """Redis-backed limiter using a fixed-window counter per bucket.

    Lightweight (one INCR + EXPIRE per acquire), shared across workers.
    Not as smooth as a real leaky bucket, but it's the standard
    practical choice for "don't get banned by IBKR" duties.
    """

    def __init__(self, url: str, specs: dict[str, BucketSpec], namespace: str = "ibkr_sentiment"):
        try:
            import redis.asyncio as redis  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError(
                "RedisRateLimiter requires the 'redis' extra. "
                "Install with: pip install -e '.[redis]'"
            ) from e
        self._redis = redis.from_url(url, decode_responses=True)
        self._specs = dict(specs)
        self._namespace = namespace

    async def acquire(self, bucket: str, tokens: int = 1) -> None:
        if bucket not in self._specs:
            raise KeyError(f"unknown rate-limit bucket: {bucket}")
        spec = self._specs[bucket]
        window = max(1.0, spec.capacity / max(1e-9, spec.refill_per_second))
        while True:
            now_window = int(time.time() // window)
            key = f"{self._namespace}:{bucket}:{now_window}"
            # INCR then check; set TTL on first increment.
            current = await self._redis.incrby(key, tokens)
            if current == tokens:
                await self._redis.expire(key, int(window) + 1)
            if current <= spec.capacity:
                return
            # Over quota — undo and wait for the next window.
            await self._redis.decrby(key, tokens)
            remaining = window - (time.time() % window)
            await asyncio.sleep(min(1.0, max(0.05, remaining)))

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:
            pass


def build_rate_limiter(
    redis_url: str | None, specs: dict[str, BucketSpec]
) -> _Limiter:
    """Pick a backend. Falls back to in-memory when no URL is given."""
    if redis_url:
        try:
            return RedisRateLimiter(redis_url, specs)
        except ImportError:
            # No redis client installed → still safe, just per-process.
            return InMemoryRateLimiter(specs)
    return InMemoryRateLimiter(specs)
