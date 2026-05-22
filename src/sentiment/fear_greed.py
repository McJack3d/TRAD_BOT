"""Crypto Fear & Greed Index sentiment source (alternative.me).

Free, no API key. The index is 0-100:
  0-24   Extreme Fear
  25-44  Fear
  45-54  Neutral
  55-74  Greed
  75-100 Extreme Greed

We read it as a MOMENTUM signal — greed = bullish — because the trend
bot is a trend follower and we want sentiment to *confirm* the trend,
not fight it. Mapping: factor = (index - 50) / 50, clamped to [-1, +1].

Honest caveat: the F&G index is itself heavily price-derived, so it
correlates with the SMA. It is unlikely to add a lot of *independent*
signal. The only way to know if it helps is to backtest SMA-alone vs
SMA+sentiment — which `history()` makes possible.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pandas as pd

from src.sentiment.base import SentimentReading, clamp_factor

FNG_URL = "https://api.alternative.me/fng/"


def index_to_factor(index: float) -> float:
    """Map a 0-100 Fear & Greed index to a [-1, +1] sentiment factor."""
    return clamp_factor((index - 50.0) / 50.0)


def index_label(index: float) -> str:
    if index < 25:
        return "Extreme Fear"
    if index < 45:
        return "Fear"
    if index < 55:
        return "Neutral"
    if index < 75:
        return "Greed"
    return "Extreme Greed"


class FearGreedSentiment:
    """Sentiment source backed by the alternative.me Fear & Greed Index."""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    async def current(self) -> SentimentReading:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(FNG_URL, params={"limit": 1})
            resp.raise_for_status()
            data = resp.json()["data"][0]
        raw = float(data["value"])
        return SentimentReading(
            value=index_to_factor(raw),
            label=data.get("value_classification") or index_label(raw),
            raw=raw,
            source="fear_greed",
            ts=datetime.fromtimestamp(int(data["timestamp"]), tz=UTC),
        )

    async def history(self) -> pd.Series:
        """Full daily history as a date-indexed Series of [-1, +1] factors.

        `limit=0` returns the entire history (since Feb 2018). Used by the
        backtester to evaluate whether sentiment actually helps.
        """
        async with httpx.AsyncClient(timeout=max(self.timeout, 20.0)) as client:
            resp = await client.get(FNG_URL, params={"limit": 0})
            resp.raise_for_status()
            rows = resp.json()["data"]
        records = {
            pd.Timestamp(int(r["timestamp"]), unit="s", tz="UTC").normalize(): index_to_factor(
                float(r["value"])
            )
            for r in rows
        }
        return pd.Series(records).sort_index()


def parse_fng_history(payload: dict) -> pd.Series:
    """Pure parser for a Fear & Greed API payload — exposed for tests so
    we don't need network access to verify the mapping."""
    records = {
        pd.Timestamp(int(r["timestamp"]), unit="s", tz="UTC").normalize(): index_to_factor(
            float(r["value"])
        )
        for r in payload["data"]
    }
    return pd.Series(records).sort_index()
