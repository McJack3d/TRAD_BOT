"""Historical data downloader wrapper to support backtesting pipelines."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import pandas as pd

from src.adapters.binance import BinanceAdapter
from src.data.history import (
    _cache_path,
    _download_funding,
    _download_ohlcv,
    _ohlcv_to_df,
    load_funding,
    load_funding_async,
    load_ohlcv,
    load_ohlcv_async,
)


class HistoricalDownloader:
    def __init__(self, exchange: BinanceAdapter, data_dir: str = "data/history"):
        self.exchange = exchange
        self.data_dir = data_dir
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)

    async def download_funding(
        self, symbol: str, start: datetime, end: datetime
    ) -> pd.Series:
        since_ms = int(start.timestamp() * 1000)
        until_ms = int(end.timestamp() * 1000)
        rows = await _download_funding(symbol, since_ms, until_ms)
        if not rows:
            return pd.Series(dtype=float)
        idx = pd.to_datetime([ts for ts, _ in rows], unit="ms", utc=True)
        s = pd.Series([r for _, r in rows], index=idx, name="funding_rate").sort_index()
        path = _cache_path(self.data_dir, symbol, "8h", "funding")
        pd.DataFrame({"funding_rate": s}).to_parquet(path)
        return s

    async def download_ohlcv(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "1h"
    ) -> pd.DataFrame:
        since_ms = int(start.timestamp() * 1000)
        until_ms = int(end.timestamp() * 1000)
        rows = await _download_ohlcv(symbol, timeframe, since_ms, until_ms)
        df = _ohlcv_to_df(rows)
        path = _cache_path(self.data_dir, symbol, timeframe, "ohlcv")
        if not df.empty:
            df.to_parquet(path)
        return df
