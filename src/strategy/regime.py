"""Regime classifier: TREND vs RANGE vs NEUTRAL.

You chose "ADX **and** realized-vol must agree" — the conservative
option. A bar is only labelled TREND when the trend is both strong
(high ADX) and the market is energetic (high realized vol), and only
RANGE when it's both weak-trend and quiet. Anything in between is
NEUTRAL, where the strategy stands aside.

Two entry points:
  * `classify_regime(adx_val, rv_pct, params)` — pure scalar, for the
    live path.
  * `classify_series(high, low, close, params)` — vectorized over a
    full OHLC history, for the backtest. Returns aligned Series of the
    regime label plus the raw ADX and rv-percentile so the backtester
    can log provenance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

import pandas as pd

from src.strategy.indicators import adx as adx_indicator
from src.strategy.indicators import realized_vol, rolling_rank_pct


class Regime(str, Enum):
    TREND = "trend"
    RANGE = "range"
    NEUTRAL = "neutral"


@dataclass(slots=True)
class RegimeParams:
    adx_window: int = 14
    adx_trend_min: float = 25.0
    adx_range_max: float = 20.0
    rv_window: int = 20
    rv_lookback: int = 200
    rv_high_pctile: float = 0.60
    rv_low_pctile: float = 0.40


def classify_regime(
    adx_val: float | None,
    rv_pct: float | None,
    params: RegimeParams,
) -> Regime:
    """Scalar classification. `rv_pct` is the percentile rank (0-1) of
    current realized vol within its lookback window. Missing inputs
    (warm-up) → NEUTRAL (stand aside until indicators are defined)."""
    if adx_val is None or rv_pct is None:
        return Regime.NEUTRAL
    try:
        a = float(adx_val)
        r = float(rv_pct)
    except (TypeError, ValueError):
        return Regime.NEUTRAL
    if math.isnan(a) or math.isnan(r):
        return Regime.NEUTRAL
    if a >= params.adx_trend_min and r >= params.rv_high_pctile:
        return Regime.TREND
    if a <= params.adx_range_max and r <= params.rv_low_pctile:
        return Regime.RANGE
    return Regime.NEUTRAL


@dataclass(slots=True)
class RegimeSeries:
    regime: pd.Series  # Regime values
    adx: pd.Series
    rv_pct: pd.Series


def classify_series(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    params: RegimeParams | None = None,
) -> RegimeSeries:
    """Compute ADX + rv-percentile over the full history and label each
    bar. Indicator columns are returned so the backtest can store why a
    bar was classified the way it was."""
    p = params or RegimeParams()
    adx_res = adx_indicator(high, low, close, window=p.adx_window)
    rv = realized_vol(close, window=p.rv_window)
    rv_pct = rolling_rank_pct(rv, p.rv_lookback)

    labels = [
        classify_regime(
            None if pd.isna(a) else a,
            None if pd.isna(r) else r,
            p,
        )
        for a, r in zip(adx_res.adx.to_numpy(), rv_pct.to_numpy(), strict=True)
    ]
    return RegimeSeries(
        regime=pd.Series(labels, index=close.index),
        adx=adx_res.adx,
        rv_pct=rv_pct,
    )
