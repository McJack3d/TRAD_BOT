"""Tests for the token-bucket rate limiter."""

from __future__ import annotations

import asyncio
import time

import pytest

from src.ibkr_sentiment.broker.rate_limiter import (
    InMemoryRateLimiter,
    build_rate_limiter,
    per_minute,
    per_window,
)


@pytest.mark.asyncio
async def test_in_memory_limiter_allows_initial_burst():
    limiter = InMemoryRateLimiter({"orders": per_minute(5)})
    start = time.monotonic()
    for _ in range(5):
        await limiter.acquire("orders")
    elapsed = time.monotonic() - start
    # Bucket starts full; 5 acquires should be near-instant.
    assert elapsed < 0.2


@pytest.mark.asyncio
async def test_in_memory_limiter_blocks_over_capacity():
    """Drain the bucket then time the next acquire."""
    limiter = InMemoryRateLimiter({"orders": per_window(2, 1.0)})
    await limiter.acquire("orders")
    await limiter.acquire("orders")
    start = time.monotonic()
    await limiter.acquire("orders")
    elapsed = time.monotonic() - start
    # Refill is 2 / 1.0 sec = 2 tokens / sec → wait ~0.5s for 1 token.
    assert elapsed >= 0.3


@pytest.mark.asyncio
async def test_unknown_bucket_raises():
    limiter = InMemoryRateLimiter({"orders": per_minute(1)})
    with pytest.raises(KeyError):
        await limiter.acquire("not_a_bucket")


@pytest.mark.asyncio
async def test_oversized_request_raises():
    limiter = InMemoryRateLimiter({"orders": per_minute(5)})
    with pytest.raises(ValueError):
        await limiter.acquire("orders", tokens=10)


def test_factory_falls_back_when_no_redis_url():
    limiter = build_rate_limiter(None, {"o": per_minute(1)})
    assert isinstance(limiter, InMemoryRateLimiter)


@pytest.mark.asyncio
async def test_parallel_acquires_serialise_per_bucket():
    """Two coroutines hammering the same bucket can't both succeed
    instantly when capacity is 1."""
    limiter = InMemoryRateLimiter({"orders": per_window(1, 0.5)})
    await limiter.acquire("orders")

    async def one():
        await limiter.acquire("orders")
        return time.monotonic()

    start = time.monotonic()
    t1, t2 = await asyncio.gather(one(), one())
    # Both finished, but at least 0.5s elapsed in total because each
    # needs its own refilled token.
    assert max(t1, t2) - start >= 0.4
