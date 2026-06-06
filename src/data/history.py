"""OHLCV + funding history loader for backtests.

Downloads perpetual-futures candles and funding rates from Binance via
ccxt, paginating through the API limits, and caches each pull to a
Parquet file under `data/history/` so repeated backtests don't re-hit
the network.

Used by `scripts/backtest_regime_switch.py`. The functions are sync
wrappers around async ccxt calls so the CLI stays simple; the heavy
lifting paginates politely with `enableRateLimit`.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pandas as pd

# ccxt timeframe → milliseconds.
_TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def _perp_symbol(symbol: str) -> str:
    """Map a spot-style 'BTC/USDT' to the USDT-M perp 'BTC/USDT:USDT'."""
    return symbol if ":" in symbol else f"{symbol}:{symbol.split('/')[1]}"


def _cache_path(cache_dir: str, symbol: str, timeframe: str, kind: str) -> Path:
    safe = symbol.replace("/", "").replace(":", "")
    return Path(cache_dir) / f"{kind}_{safe}_{timeframe}.parquet"


async def _download_ohlcv_async(
    symbol: str, timeframe: str, since_ms: int, until_ms: int
) -> list[list[float]]:
    import ccxt.async_support as ccxt  # type: ignore[import-untyped]

    ex = ccxt.binanceusdm({"enableRateLimit": True})
    out: list[list[float]] = []
    perp = _perp_symbol(symbol)
    step = _TF_MS[timeframe]
    cursor = since_ms
    try:
        await ex.load_markets()
        while cursor < until_ms:
            batch = await _retry(
                lambda c=cursor: ex.fetch_ohlcv(perp, timeframe, since=c, limit=1500)
            )
            if not batch:
                break
            out.extend(batch)
            cursor = batch[-1][0] + step
            if len(batch) < 1500:
                break
    finally:
        await ex.close()
    return [r for r in out if r[0] < until_ms]


async def _download_funding_async(
    symbol: str, since_ms: int, until_ms: int
) -> list[tuple[int, float]]:
    import ccxt.async_support as ccxt  # type: ignore[import-untyped]

    ex = ccxt.binanceusdm({"enableRateLimit": True})
    perp = _perp_symbol(symbol)
    out: list[tuple[int, float]] = []
    cursor = since_ms
    try:
        await ex.load_markets()
        while cursor < until_ms:
            batch = await _retry(
                lambda c=cursor: ex.fetch_funding_rate_history(perp, since=c, limit=1000)
            )
            if not batch:
                break
            for row in batch:
                ts = int(row["timestamp"])
                rate = float(row["fundingRate"])
                out.append((ts, rate))
            last = int(batch[-1]["timestamp"])
            cursor = last + 1
            if len(batch) < 1000:
                break
    finally:
        await ex.close()
    return [(ts, r) for ts, r in out if ts < until_ms]


async def _retry(coro_factory, attempts: int = 4):
    last: Exception | None = None
    for k in range(attempts):
        try:
            return await coro_factory()
        except Exception as e:  # noqa: BLE001
            last = e
            await asyncio.sleep(2**k)
    assert last is not None
    raise last


def _ohlcv_to_df(rows: list[list[float]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset="ts").sort_values("ts")
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df[["open", "high", "low", "close", "volume"]]


def load_ohlcv(
    symbol: str,
    timeframe: str = "1h",
    months: int = 6,
    cache_dir: str = "data/history",
    refresh: bool = False,
) -> pd.DataFrame:
    """Load `months` of candles for `symbol` at `timeframe`. Cached to
    Parquet; pass refresh=True to force a re-download."""
    if timeframe not in _TF_MS:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, symbol, timeframe, "ohlcv")
    if path.exists() and not refresh:
        return pd.read_parquet(path)

    until_ms = int(time.time() * 1000)
    since_ms = until_ms - months * 30 * 86_400_000
    rows = asyncio.run(_download_ohlcv_async(symbol, timeframe, since_ms, until_ms))
    df = _ohlcv_to_df(rows)
    if not df.empty:
        df.to_parquet(path)
    return df


def load_funding(
    symbol: str,
    months: int = 6,
    cache_dir: str = "data/history",
    refresh: bool = False,
) -> pd.Series:
    """Load funding-rate history as a ts-indexed Series (rate per 8h)."""
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, symbol, "8h", "funding")
    if path.exists() and not refresh:
        s = pd.read_parquet(path)["funding_rate"]
        s.index = pd.to_datetime(s.index, utc=True)
        return s

    until_ms = int(time.time() * 1000)
    since_ms = until_ms - months * 30 * 86_400_000
    rows = asyncio.run(_download_funding_async(symbol, since_ms, until_ms))
    if not rows:
        return pd.Series(dtype=float)
    idx = pd.to_datetime([ts for ts, _ in rows], unit="ms", utc=True)
    s = pd.Series([r for _, r in rows], index=idx, name="funding_rate").sort_index()
    pd.DataFrame({"funding_rate": s}).to_parquet(path)
    return s
