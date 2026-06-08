"""OHLCV + funding history loader for backtests.

Downloads perpetual-futures candles and funding rates from Binance via
ccxt, paginating through the API limits, and caches each pull to a
Parquet file under `data/history/` so repeated backtests don't re-hit
the network.

Two parallel APIs:

  * `load_ohlcv_async` / `load_funding_async` — the canonical async
    functions. Use these from anywhere already in an asyncio context
    (e.g. the tradbot CLI handlers).
  * `load_ohlcv` / `load_funding` — thin sync wrappers that do a single
    `asyncio.run(...)` for standalone scripts that aren't already in a
    loop.

Calling the sync wrappers from inside a running event loop is a
programming error (Python forbids nested `asyncio.run`). They raise a
clear `RuntimeError` pointing at the async variant rather than the
opaque "cannot be called from a running event loop" the runtime
produces, which was the bug behind the misleading "geo-blocked"
message in the backtest CLI on first deploy.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pandas as pd

# 365 days in milliseconds — annualises the period-based borrow rate
# Binance returns from its interest-rate history (quoted daily).
_MS_PER_YEAR = 31_536_000_000

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


# ---- downloaders -------------------------------------------------------


async def _download_ohlcv(
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


async def _download_funding(
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


def _binance_credentials() -> tuple[str, str]:
    """Resolve Binance API credentials. Checks real environment variables
    first (cheap, no import), then falls back to the project's `Secrets`
    config which also reads the `.env` file — the key fix: keys placed in
    `.env` are loaded by pydantic into `Secrets`, NOT exported to
    `os.environ`, so an env-only check never sees them. Returns
    (key, secret); either may be empty."""
    import os

    key = os.environ.get("BINANCE_API_KEY") or ""
    secret = os.environ.get("BINANCE_API_SECRET") or ""
    if key and secret:
        return key, secret
    try:
        from src.config import Secrets

        s = Secrets()
        return (key or s.binance_api_key or "", secret or s.binance_api_secret or "")
    except Exception:  # noqa: BLE001 — config import/parse must not crash the loader
        return key, secret


async def _download_borrow_rate(
    asset: str, since_ms: int, until_ms: int
) -> list[tuple[int, float]]:
    """Paginate Binance's cross-margin interest-rate history for `asset`.

    Unlike OHLCV and funding, `fetch_borrow_rate_history` is an
    **authenticated** endpoint — Binance requires a real API key/secret
    even for read-only access. Credentials are resolved via the project's
    `Secrets` config (the same source the trend bot uses), which reads
    both real env vars AND the `.env` file; a clear `RuntimeError` is
    raised if neither has them, so the CLI prints "set your keys" rather
    than an opaque ccxt AuthenticationError.

    ccxt's `fetch_borrow_rate_history` returns the rate over a `period`
    (Binance quotes daily, period = 86_400_000 ms). We annualise each
    point to APR so the carry math compares it like-for-like against the
    per-8h funding. Binance caps `limit` at 92 (≈3 months of daily
    points), so we paginate by advancing the cursor to the last timestamp
    seen — with a no-forward-progress guard against an infinite loop."""
    import ccxt.async_support as ccxt  # type: ignore[import-untyped]

    api_key, api_secret = _binance_credentials()
    if not api_key or not api_secret:
        raise RuntimeError(
            "BINANCE_API_KEY / BINANCE_API_SECRET not set — Binance's "
            "borrow-rate history is an authenticated endpoint. Add them "
            "to your .env (the same keys the trend bot uses; a read-only, "
            "no-withdraw key is enough)."
        )

    ex = ccxt.binance({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
    })
    out: list[tuple[int, float]] = []
    cursor = since_ms
    try:
        await ex.load_markets()
        while cursor < until_ms:
            batch = await _retry(
                lambda c=cursor: ex.fetch_borrow_rate_history(asset, since=c, limit=92)
            )
            if not batch:
                break
            for row in batch:
                ts = int(row["timestamp"])
                period_ms = row.get("period") or 86_400_000
                apr = float(row["rate"]) * (_MS_PER_YEAR / period_ms)
                out.append((ts, apr))
            last = int(batch[-1]["timestamp"])
            if last + 1 <= cursor:  # no forward progress — stop
                break
            cursor = last + 1
            if len(batch) < 92:
                break
    finally:
        await ex.close()
    # Dedup on timestamp, clip to window, sort.
    clipped = {ts: apr for ts, apr in out if ts < until_ms}
    return sorted(clipped.items())


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


# ---- async public API (canonical) --------------------------------------


# Override hooks for tests — replace these to feed canned data and avoid
# any network. Production code should leave them alone.
_OHLCV_FETCHER = _download_ohlcv
_FUNDING_FETCHER = _download_funding
_BORROW_RATE_FETCHER = _download_borrow_rate


async def load_ohlcv_async(
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
    rows = await _OHLCV_FETCHER(symbol, timeframe, since_ms, until_ms)
    df = _ohlcv_to_df(rows)
    if not df.empty:
        df.to_parquet(path)
    return df


async def load_funding_async(
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
    rows = await _FUNDING_FETCHER(symbol, since_ms, until_ms)
    if not rows:
        return pd.Series(dtype=float)
    idx = pd.to_datetime([ts for ts, _ in rows], unit="ms", utc=True)
    s = pd.Series([r for _, r in rows], index=idx, name="funding_rate").sort_index()
    pd.DataFrame({"funding_rate": s}).to_parquet(path)
    return s


async def load_borrow_rate_async(
    asset: str,
    months: int = 6,
    cache_dir: str = "data/history",
    refresh: bool = False,
) -> pd.Series:
    """Load cross-margin borrow-rate history for `asset` (e.g. "BTC") as a
    ts-indexed Series of APR — 0.06 means 6 % annualised. This is the
    negative leg's cost series; the backtester nets it against |funding|.

    Cached to Parquet alongside the OHLCV/funding pulls."""
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, asset, "1d", "borrow")
    if path.exists() and not refresh:
        s = pd.read_parquet(path)["borrow_rate_apr"]
        s.index = pd.to_datetime(s.index, utc=True)
        return s

    until_ms = int(time.time() * 1000)
    since_ms = until_ms - months * 30 * 86_400_000
    rows = await _BORROW_RATE_FETCHER(asset, since_ms, until_ms)
    if not rows:
        return pd.Series(dtype=float)
    idx = pd.to_datetime([ts for ts, _ in rows], unit="ms", utc=True)
    s = pd.Series(
        [r for _, r in rows], index=idx, name="borrow_rate_apr"
    ).sort_index()
    pd.DataFrame({"borrow_rate_apr": s}).to_parquet(path)
    return s


# ---- sync wrappers (for standalone scripts) ----------------------------


def _running_loop_or_none():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _ensure_no_running_loop(funcname: str) -> None:
    """Raise a clear error if a sync wrapper is invoked from inside an
    event loop. Python's own error message ('asyncio.run() cannot be
    called from a running event loop') buries the cause."""
    if _running_loop_or_none() is not None:
        raise RuntimeError(
            f"{funcname}() is the sync wrapper; it can't be called from "
            f"inside an asyncio event loop. Use `await {funcname}_async(...)` "
            f"instead."
        )


def load_ohlcv(
    symbol: str,
    timeframe: str = "1h",
    months: int = 6,
    cache_dir: str = "data/history",
    refresh: bool = False,
) -> pd.DataFrame:
    """Sync wrapper around `load_ohlcv_async`. For standalone scripts
    only — inside an event loop, use `load_ohlcv_async`."""
    _ensure_no_running_loop("load_ohlcv")
    return asyncio.run(
        load_ohlcv_async(symbol, timeframe, months, cache_dir, refresh)
    )


def load_funding(
    symbol: str,
    months: int = 6,
    cache_dir: str = "data/history",
    refresh: bool = False,
) -> pd.Series:
    """Sync wrapper around `load_funding_async`. For standalone scripts
    only — inside an event loop, use `load_funding_async`."""
    _ensure_no_running_loop("load_funding")
    return asyncio.run(load_funding_async(symbol, months, cache_dir, refresh))


def load_borrow_rate(
    asset: str,
    months: int = 6,
    cache_dir: str = "data/history",
    refresh: bool = False,
) -> pd.Series:
    """Sync wrapper around `load_borrow_rate_async`. For standalone
    scripts only — inside an event loop, use `load_borrow_rate_async`."""
    _ensure_no_running_loop("load_borrow_rate")
    return asyncio.run(load_borrow_rate_async(asset, months, cache_dir, refresh))
