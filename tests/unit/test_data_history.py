"""Tests for the OHLCV + funding loader.

The most important test here is the asyncio-nesting regression: the
first build's `load_ohlcv` did `asyncio.run(...)` internally, which
explodes when called from inside a running event loop (i.e. from the
tradbot CLI). These tests pin down the contract:

  * `load_ohlcv_async` works from inside an event loop.
  * `load_ohlcv` (the sync wrapper) raises a CLEAR error when misused.
  * The cache is honoured on both paths.
"""

from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from src.data import history as histmod
from src.data.history import (
    load_funding,
    load_funding_async,
    load_ohlcv,
    load_ohlcv_async,
)

_FAKE_TS = 1_700_000_000_000  # an arbitrary ms epoch within sane range


def _make_ohlcv_rows(n: int = 500, step_ms: int = 3_600_000) -> list[list[float]]:
    return [
        [_FAKE_TS + i * step_ms, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 1000.0]
        for i in range(n)
    ]


def _make_funding_rows(n: int = 30) -> list[tuple[int, float]]:
    step = 8 * 3_600_000
    return [(_FAKE_TS + i * step, 0.0001) for i in range(n)]


@pytest.fixture(autouse=True)
def _stub_downloaders(monkeypatch):
    """Replace the ccxt downloaders with deterministic stubs so the
    tests never touch the network."""

    async def fake_ohlcv(symbol, timeframe, since_ms, until_ms):
        return _make_ohlcv_rows()

    async def fake_funding(symbol, since_ms, until_ms):
        return _make_funding_rows()

    monkeypatch.setattr(histmod, "_OHLCV_FETCHER", fake_ohlcv)
    monkeypatch.setattr(histmod, "_FUNDING_FETCHER", fake_funding)


@pytest.mark.asyncio
async def test_load_ohlcv_async_works_inside_running_loop(tmp_path):
    """REGRESSION: the v1 sync wrapper crashed with 'asyncio.run cannot
    be called from a running event loop' the moment it was invoked from
    inside the tradbot async handler. The async API must just work."""
    df = await load_ohlcv_async(
        "BTC/USDT", "1h", months=1, cache_dir=str(tmp_path)
    )
    assert not df.empty
    assert {"open", "high", "low", "close", "volume"} == set(df.columns)
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.tz is not None  # UTC


@pytest.mark.asyncio
async def test_load_ohlcv_async_uses_cache(tmp_path, monkeypatch):
    df1 = await load_ohlcv_async("BTC/USDT", "1h", months=1, cache_dir=str(tmp_path))
    assert not df1.empty

    # Swap the fetcher for one that would fail; the cache should mean it
    # never gets called.
    called = {"n": 0}

    async def explode(*a, **kw):
        called["n"] += 1
        raise RuntimeError("fetcher should not be called when cache exists")

    monkeypatch.setattr(histmod, "_OHLCV_FETCHER", explode)
    df2 = await load_ohlcv_async("BTC/USDT", "1h", months=1, cache_dir=str(tmp_path))
    pd.testing.assert_frame_equal(df1, df2)
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_load_ohlcv_async_refresh_bypasses_cache(tmp_path, monkeypatch):
    await load_ohlcv_async("BTC/USDT", "1h", months=1, cache_dir=str(tmp_path))
    called = {"n": 0}

    async def counting(*a, **kw):
        called["n"] += 1
        return _make_ohlcv_rows()

    monkeypatch.setattr(histmod, "_OHLCV_FETCHER", counting)
    await load_ohlcv_async(
        "BTC/USDT", "1h", months=1, cache_dir=str(tmp_path), refresh=True
    )
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_load_funding_async(tmp_path):
    s = await load_funding_async("BTC/USDT", months=1, cache_dir=str(tmp_path))
    assert not s.empty
    assert s.index.tz is not None


def test_sync_load_ohlcv_works_standalone(tmp_path):
    """The sync wrapper is for standalone scripts — no running loop."""
    df = load_ohlcv("BTC/USDT", "1h", months=1, cache_dir=str(tmp_path))
    assert not df.empty


def test_sync_load_funding_works_standalone(tmp_path):
    s = load_funding("BTC/USDT", months=1, cache_dir=str(tmp_path))
    assert not s.empty


@pytest.mark.asyncio
async def test_sync_wrapper_inside_loop_raises_clear_error(tmp_path):
    """The sync wrapper must give a SPECIFIC error pointing at the
    async variant — not the generic 'asyncio.run cannot be called from
    a running event loop' that hid the real cause for a release."""
    with pytest.raises(RuntimeError) as exc:
        load_ohlcv("BTC/USDT", "1h", months=1, cache_dir=str(tmp_path))
    msg = str(exc.value)
    assert "event loop" in msg
    assert "load_ohlcv_async" in msg  # tells the caller what to do


def test_unsupported_timeframe_raises(tmp_path):
    with pytest.raises(ValueError, match="unsupported timeframe"):
        asyncio.run(
            load_ohlcv_async(
                "BTC/USDT", "3m", months=1, cache_dir=str(tmp_path)
            )
        )
