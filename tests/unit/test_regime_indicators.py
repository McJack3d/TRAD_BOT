"""Tests for the ATR / ADX / volatility indicators."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.strategy.indicators import (
    adx,
    atr,
    realized_vol,
    rolling_rank_pct,
    true_range,
)


def test_true_range_basic():
    high = pd.Series([10.0, 11.0, 12.0])
    low = pd.Series([9.0, 9.5, 10.0])
    close = pd.Series([9.5, 10.5, 11.5])
    tr = true_range(high, low, close)
    # bar 0: only H-L (no prev close) = 1.0
    assert tr.iloc[0] == 1.0
    # bar 1: max(11-9.5, |11-9.5|, |9.5-9.5|) = max(1.5, 1.5, 0) = 1.5
    assert tr.iloc[1] == 1.5


def test_atr_is_positive_and_tracks_volatility():
    n = 100
    # Low-vol then high-vol regime.
    rng = np.random.default_rng(0)
    calm = 100 + np.cumsum(rng.normal(0, 0.2, n))
    wild = calm[-1] + np.cumsum(rng.normal(0, 2.0, n))
    close = pd.Series(np.concatenate([calm, wild]))
    high = close + 0.5
    low = close - 0.5
    a = atr(high, low, close, window=14)
    assert (a.dropna() > 0).all()
    # ATR in the wild half should exceed the calm half.
    assert a.iloc[150] > a.iloc[80]


def test_adx_high_in_trend_low_in_chop():
    n = 200
    # Strong, smooth uptrend → high ADX.
    trend_close = pd.Series(np.linspace(100, 200, n))
    th = trend_close + 0.5
    tl = trend_close - 0.5
    trend_adx = adx(th, tl, trend_close, window=14).adx.dropna()

    # Choppy oscillation → low ADX.
    chop_close = pd.Series(100 + 2 * np.sin(np.arange(n) * 0.5))
    ch = chop_close + 0.5
    cl = chop_close - 0.5
    chop_adx = adx(ch, cl, chop_close, window=14).adx.dropna()

    assert trend_adx.iloc[-1] > 40  # strong trend reads high
    assert chop_adx.iloc[-1] < 30   # chop reads lower
    assert trend_adx.iloc[-1] > chop_adx.iloc[-1]


def test_adx_plus_di_dominates_in_uptrend():
    n = 100
    close = pd.Series(np.linspace(100, 150, n))
    res = adx(close + 0.5, close - 0.5, close, window=14)
    # In a clean uptrend +DI should sit above -DI.
    assert res.plus_di.iloc[-1] > res.minus_di.iloc[-1]


def test_realized_vol_orders_correctly():
    rng = np.random.default_rng(1)
    calm = pd.Series(100 + np.cumsum(rng.normal(0, 0.1, 200)))
    wild = pd.Series(100 + np.cumsum(rng.normal(0, 3.0, 200)))
    assert realized_vol(wild, 20).iloc[-1] > realized_vol(calm, 20).iloc[-1]


def test_rolling_rank_pct_top_and_bottom():
    s = pd.Series(range(100), dtype=float)  # strictly increasing
    rk = rolling_rank_pct(s, 20)
    # Every value is the max of its trailing window → rank ~1.0.
    assert rk.iloc[-1] == 1.0
    # A strictly decreasing series → current is the min → rank near 1/window.
    s2 = pd.Series(range(100, 0, -1), dtype=float)
    rk2 = rolling_rank_pct(s2, 20)
    assert rk2.iloc[-1] <= 0.05
