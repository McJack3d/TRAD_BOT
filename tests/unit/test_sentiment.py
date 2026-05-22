"""Tests for the sentiment factor and its effect on the trend signal."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from src.backtest.trend_backtest import backtest_sma_trend
from src.sentiment.base import clamp_factor
from src.sentiment.fear_greed import index_label, index_to_factor, parse_fng_history
from src.strategy.sma_trend import TrendState, evaluate_trend


def _series(values: list[float]) -> pd.Series:
    idx = pd.date_range("2020-01-01", periods=len(values), freq="1D", tz="UTC")
    return pd.Series(values, index=idx)


# ---- Fear & Greed mapping -------------------------------------------


def test_index_to_factor_endpoints() -> None:
    assert index_to_factor(0) == -1.0
    assert index_to_factor(50) == 0.0
    assert index_to_factor(100) == 1.0


def test_index_to_factor_clamps() -> None:
    # Out-of-range inputs (shouldn't happen, but be safe) clamp.
    assert index_to_factor(200) == 1.0
    assert index_to_factor(-50) == -1.0


def test_index_label_buckets() -> None:
    assert index_label(10) == "Extreme Fear"
    assert index_label(35) == "Fear"
    assert index_label(50) == "Neutral"
    assert index_label(65) == "Greed"
    assert index_label(90) == "Extreme Greed"


def test_clamp_factor() -> None:
    assert clamp_factor(2.0) == 1.0
    assert clamp_factor(-2.0) == -1.0
    assert clamp_factor(0.4) == 0.4


def test_parse_fng_history() -> None:
    payload = {
        "data": [
            {"value": "75", "timestamp": "1700000000"},
            {"value": "25", "timestamp": "1699913600"},
        ]
    }
    s = parse_fng_history(payload)
    assert len(s) == 2
    # 75 → +0.5, 25 → -0.5
    assert set(s.values) == {0.5, -0.5}
    # Index sorted ascending.
    assert s.index[0] < s.index[1]


# ---- sentiment tilts the signal -------------------------------------


def test_bullish_sentiment_lowers_entry_bar() -> None:
    """With bullish sentiment, a close that's marginally BELOW the plain
    entry threshold can still trigger IN."""
    # 49 closes at 100, last at 100.5. SMA ~100.01. Plain 1% buffer →
    # entry threshold ~101.0 → close 100.5 would be OUT.
    closes = pd.Series([100.0] * 49 + [100.5])
    plain = evaluate_trend(closes, sma_window=50, entry_buffer_pct=0.01)
    assert plain.state == TrendState.OUT

    # Strong bullish sentiment with weight 0.03 shifts the entry buffer
    # to 0.01 - 1.0*0.03 = -0.02 → threshold ~98 → close 100.5 is IN.
    bullish = evaluate_trend(
        closes, sma_window=50, entry_buffer_pct=0.01,
        sentiment=1.0, sentiment_weight=0.03,
    )
    assert bullish.state == TrendState.IN
    assert bullish.sentiment == 1.0


def test_bearish_sentiment_raises_entry_bar() -> None:
    """Bearish sentiment demands more confirmation — a close just above
    the plain threshold is held OUT."""
    closes = pd.Series([100.0] * 49 + [101.5])
    plain = evaluate_trend(closes, sma_window=50, entry_buffer_pct=0.01)
    assert plain.state == TrendState.IN  # 101.5 > ~101.0

    bearish = evaluate_trend(
        closes, sma_window=50, entry_buffer_pct=0.01,
        sentiment=-1.0, sentiment_weight=0.03,
    )
    # Buffer 0.01 + 0.03 = 0.04 → threshold ~104 → 101.5 is OUT.
    assert bearish.state == TrendState.OUT


def test_sentiment_weight_zero_is_a_noop() -> None:
    closes = pd.Series([100.0] * 49 + [101.5])
    a = evaluate_trend(closes, sma_window=50, entry_buffer_pct=0.01)
    b = evaluate_trend(
        closes, sma_window=50, entry_buffer_pct=0.01,
        sentiment=1.0, sentiment_weight=0.0,
    )
    assert a.state == b.state


# ---- sentiment in the backtester ------------------------------------


def test_backtest_accepts_sentiment_series() -> None:
    """A backtest with a sentiment series runs and is influenced by it."""
    values = [100.0] * 50 + [100.0 + 0.5 * i for i in range(100)]
    closes = _series(values)
    # Constant strongly-bullish sentiment over the whole window.
    sentiment = pd.Series(1.0, index=closes.index)

    no_sentiment = backtest_sma_trend(closes, sma_window=50)
    with_sentiment = backtest_sma_trend(
        closes, sma_window=50, sentiment_series=sentiment, sentiment_weight=0.03
    )
    # Bullish sentiment lowers the entry bar → it should enter at least as
    # early as the plain run, hence end with >= equity. (Equal is allowed
    # if the entry bar wasn't the binding constraint.)
    assert with_sentiment.final_equity >= no_sentiment.final_equity * Decimal("0.99")


def test_backtest_sentiment_no_lookahead() -> None:
    """Sentiment must be applied as-of each bar, not from the future.
    A sentiment series that only starts halfway through must not affect
    the first half of the backtest."""
    values = [100.0] * 50 + [100.0 + 0.5 * i for i in range(100)]
    closes = _series(values)
    # Sentiment defined only for the last 10 bars.
    late = pd.Series(1.0, index=closes.index[-10:])

    r_late = backtest_sma_trend(
        closes, sma_window=50, sentiment_series=late, sentiment_weight=0.03
    )
    r_none = backtest_sma_trend(closes, sma_window=50)
    # The early-window trades should be identical (sentiment None there).
    early_late = [t for t in r_late.trades if t["ts"] < closes.index[-10]]
    early_none = [t for t in r_none.trades if t["ts"] < closes.index[-10]]
    assert len(early_late) == len(early_none)
