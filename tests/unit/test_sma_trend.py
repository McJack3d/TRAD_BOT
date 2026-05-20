"""SMA trend-following signal tests."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from src.strategy.sma_trend import TrendState, evaluate_trend


def test_close_above_sma_returns_in() -> None:
    # 50 closes at 100, last close at 200 → close >> SMA50
    closes = pd.Series([100.0] * 49 + [200.0])
    sig = evaluate_trend(closes, sma_window=50)
    assert sig.state == TrendState.IN
    assert sig.close == Decimal("200.0")


def test_close_below_sma_returns_out() -> None:
    closes = pd.Series([200.0] * 49 + [100.0])
    sig = evaluate_trend(closes, sma_window=50)
    assert sig.state == TrendState.OUT


def test_close_equal_to_sma_returns_out() -> None:
    """Equality goes to OUT — strictly greater than to enter."""
    closes = pd.Series([100.0] * 50)
    sig = evaluate_trend(closes, sma_window=50)
    assert sig.state == TrendState.OUT


def test_too_few_bars_returns_out() -> None:
    closes = pd.Series([100.0] * 10)
    sig = evaluate_trend(closes, sma_window=50)
    assert sig.state == TrendState.OUT
    assert "need 50" in sig.reason


def test_realistic_btc_uptrend() -> None:
    # Linearly rising series from 20k to 60k → close above SMA50.
    closes = pd.Series([20000 + 800 * i for i in range(50)])
    sig = evaluate_trend(closes, sma_window=50)
    assert sig.state == TrendState.IN


def test_realistic_btc_downtrend() -> None:
    closes = pd.Series([60000 - 800 * i for i in range(50)])
    sig = evaluate_trend(closes, sma_window=50)
    assert sig.state == TrendState.OUT
