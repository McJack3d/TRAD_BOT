"""Tests for the regime classifier."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.strategy.regime import (
    Regime,
    RegimeParams,
    classify_regime,
    classify_series,
)


def test_classify_scalar_trend():
    p = RegimeParams()
    # Strong ADX + high vol percentile → TREND.
    assert classify_regime(30.0, 0.8, p) == Regime.TREND


def test_classify_scalar_range():
    p = RegimeParams()
    # Weak ADX + low vol percentile → RANGE.
    assert classify_regime(15.0, 0.2, p) == Regime.RANGE


def test_classify_scalar_neutral_when_indicators_disagree():
    p = RegimeParams()
    # Strong ADX but LOW vol → they disagree → NEUTRAL (the "both must
    # agree" rule).
    assert classify_regime(30.0, 0.2, p) == Regime.NEUTRAL
    # Weak ADX but HIGH vol → also NEUTRAL.
    assert classify_regime(15.0, 0.9, p) == Regime.NEUTRAL


def test_classify_scalar_handles_missing_and_nan():
    p = RegimeParams()
    assert classify_regime(None, 0.8, p) == Regime.NEUTRAL
    assert classify_regime(30.0, None, p) == Regime.NEUTRAL
    assert classify_regime(float("nan"), 0.8, p) == Regime.NEUTRAL


def test_classify_series_labels_a_clean_trend_as_trend():
    n = 400
    # Smooth strong uptrend with enough vol to clear the rv gate.
    rng = np.random.default_rng(3)
    close = pd.Series(np.linspace(100, 300, n) + rng.normal(0, 1.0, n))
    res = classify_series(close + 0.5, close - 0.5, close)
    # By the end of a long clean trend, the dominant late-window label
    # should be TREND at least sometimes (not NEUTRAL everywhere).
    tail = res.regime.iloc[-50:]
    assert (tail == Regime.TREND).sum() > 0


def test_classify_series_warmup_is_neutral():
    close = pd.Series(np.linspace(100, 110, 50))
    res = classify_series(close + 0.5, close - 0.5, close)
    # Before indicators warm up, everything is NEUTRAL.
    assert res.regime.iloc[0] == Regime.NEUTRAL
    assert len(res.regime) == 50
