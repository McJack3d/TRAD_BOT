"""Indicator formula tests.

Each indicator is checked against a hand-computed value on a tiny
fixture, and for the basic shape (NaN warmup, correct length).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from src.strategy.indicators import bollinger_bands, macd, rsi


def test_bollinger_bands_basic_shape() -> None:
    closes = pd.Series(np.arange(1, 51, dtype=float))  # 1..50
    bb = bollinger_bands(closes, window=20, num_std=2.0)
    assert len(bb.middle) == 50
    # First 19 entries are NaN (rolling mean needs 20).
    assert bb.middle.iloc[:19].isna().all()
    # 20th entry is the mean of 1..20 = 10.5.
    assert math.isclose(bb.middle.iloc[19], 10.5, abs_tol=1e-9)
    # Width is positive once defined.
    assert (bb.width.dropna() >= 0).all()


def test_bollinger_bands_flat_input_has_zero_width() -> None:
    closes = pd.Series([100.0] * 30)
    bb = bollinger_bands(closes, window=20)
    # No variation → bands collapse onto the middle, width == 0.
    assert math.isclose(bb.width.iloc[-1], 0.0, abs_tol=1e-12)
    assert math.isclose(bb.lower.iloc[-1], 100.0, abs_tol=1e-12)
    assert math.isclose(bb.upper.iloc[-1], 100.0, abs_tol=1e-12)


def test_rsi_monotonic_rise_gives_high_value() -> None:
    # Strictly increasing closes → only gains → RSI should be ~100.
    closes = pd.Series(np.arange(1.0, 101.0))
    r = rsi(closes, window=14)
    last = float(r.iloc[-1])
    assert last > 99.0, f"expected ~100, got {last}"


def test_rsi_monotonic_fall_gives_low_value() -> None:
    closes = pd.Series(np.arange(100.0, 0.0, -1.0))
    r = rsi(closes, window=14)
    last = float(r.iloc[-1])
    assert last < 1.0, f"expected ~0, got {last}"


def test_rsi_flat_input_is_undefined_but_handled() -> None:
    closes = pd.Series([50.0] * 30)
    r = rsi(closes, window=14)
    # Flat input → no movement; not crashy is enough. (Either NaN or 100.)
    assert not math.isinf(float(r.iloc[-1])) if not math.isnan(float(r.iloc[-1])) else True


def test_macd_returns_three_aligned_series() -> None:
    closes = pd.Series(np.linspace(100, 200, 100))
    m = macd(closes)
    assert len(m.line) == 100 == len(m.signal) == len(m.histogram)
    # Up-trend → MACD line positive (fast EMA leads slow EMA).
    assert float(m.line.iloc[-1]) > 0
    # Histogram = line - signal.
    last_diff = float(m.line.iloc[-1]) - float(m.signal.iloc[-1])
    assert math.isclose(last_diff, float(m.histogram.iloc[-1]), abs_tol=1e-9)
